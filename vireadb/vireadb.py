#! /usr/bin/env python3
'''
vireadb: Viral Read Database
'''

# imports
from .common import *
from .compress import *
from .cram import *
from .fasta import *
from io import BytesIO # TODO DELETE
from lzma import LZMACompressor, LZMADecompressor, PRESET_EXTREME # TODO DELETE
from os import remove
from os.path import isdir, isfile
from subprocess import call, check_output, DEVNULL, PIPE, Popen
from sys import argv
from tempfile import NamedTemporaryFile
from warnings import warn
import argparse
import numpy
import sqlite3

# constants
META_TABLE_COLS = ('key', 'val')
SEQS_TABLE_COLS = ('ID', 'CRAM', 'POS_COUNTS_XZ', 'INS_COUNTS_XZ', 'CONSENSUS_XZ')

# base commands
BASE_COMMAND_SAMTOOLS_VIEW_CRAM = [
    'samtools', 'view',
    '--output-fmt-option', 'version=3.0', # TODO update to 3.1 when stable
    '--output-fmt-option', 'use_lzma=1',
    '--output-fmt-option', 'archive=1',
    '--output-fmt-option', 'level=9',
    '--output-fmt-option', 'lossy_names=1',
    '-C', # CRAM output
]
BASE_COMMAND_MINIMAP2 = [
    'minimap2',
    '-x', 'sr',
]

class ViReaDB:
    '''``ViReaDB`` database class'''
    def __init__(self, db_fn, bufsize=DEFAULT_BUFSIZE):
        '''``ViReaDB`` constructor

        Args:
            ``db_fn`` (``str``): The filename of the SQLite3 database file representing this database

            ``bufsize`` (``int``): Buffer size for reading from file

        Returns:
            ``ViReaDB`` object
        '''
        self.con = sqlite3.connect(db_fn)
        self.cur = self.con.cursor()
        self.version = self.cur.execute("SELECT val FROM meta WHERE key='VERSION'").fetchone()[0]
        self.ref_name = self.cur.execute("SELECT val FROM meta WHERE key='REF_NAME'").fetchone()[0]
        ref_seq_xz = self.cur.execute("SELECT val FROM meta WHERE key='REF_SEQ_XZ'").fetchone()[0]
        self.ref_seq = decompress_seq(ref_seq_xz)
        self.ref_len = len(self.ref_seq)
        self.ref_f = NamedTemporaryFile('w', prefix='vireadb', suffix='.fas', buffering=bufsize)
        self.ref_f.write('%s\n%s\n' % (self.ref_name, self.ref_seq)); self.ref_f.flush()
        self.mmi_f = NamedTemporaryFile('wb', prefix='vireadb', suffix='.mmi', buffering=bufsize)
        self.mmi_f.write(self.cur.execute("SELECT val FROM meta WHERE key='REF_MMI'").fetchone()[0]); self.mmi_f.flush()

    def __del__(self):
        '''``ViReaDB`` destructor'''
        self.con.close()

    def __len__(self):
        '''Return the number of entries in this database

        Returns:
            The number of entries in this database
        '''
        return self.cur.execute("SELECT COUNT(*) FROM seqs").fetchone()[0]

    def __getitem__(self, ID):
        '''Shorthand for get_entry(ID)'''
        return self.get_entry(ID)

    def __delitem__(self, ID):
        '''Shorthand for del_entry(ID)'''
        self.del_entry(ID)

    def commit(self):
        '''Commit the SQLite3 database'''
        self.con.commit()

    def add_entry(self, ID, reads_fn, filetype=None, include_unmapped=False, bufsize=DEFAULT_BUFSIZE, threads=DEFAULT_THREADS, commit=True):
        '''Add a CRAM/BAM/SAM/FASTQ entry to this database. CRAM inputs are added exactly as-is.

        Args:
            ``ID`` (``str``): The unique ID of the entry to add

            ``reads_fn`` (``str``): The input reads file. Can provide list of multiple files if FASTQ

            ``filetype`` (``str``): The format of the input reads file (CRAM, BAM, SAM, or FASTQ), or None to infer from ``reads_fn``

            ``include_unmapped`` (``bool``): Include unmapped reads when converting from non-CRAM formats

            ``bufsize`` (``int``): Buffer size for reading from file
            
            ``threads`` (``int``): Number of threads to use for compression

            ``commit`` (``bool``): Commit database after adding this entry
        '''
        # check for validity
        if self.cur.execute("SELECT ID FROM seqs WHERE ID='%s'" % ID).fetchone() is not None:
            raise ValueError("ID already exists in database: %s" % ID)
        if isinstance(reads_fn, list):
            if len(reads_fn) == 0:
                raise ValueError("Must specify at least 1 reads file")
            elif len(reads_fn) == 1:
                reads_fn = reads_fn[0]
        if isinstance(reads_fn, str) and not isfile(reads_fn):
            raise ValueError("File not found: %s" % reads_fn)
        if filetype is None:
            if isinstance(reads_fn, str):
                filetype = reads_fn.upper().rstrip('.GZ').split('.')[-1]
            else:
                for fn in reads_fn:
                    if filetype is None:
                        filetype = fn.upper().rstrip('.GZ').split('.')[-1]
                    elif filetype != fn.upper().rstrip('.GZ').split('.')[-1]:
                        raise ValueError("All reads_fn arguments must be the same filetype")
        if not isinstance(filetype, str):
            raise TypeError("Invalid filetype: %s (must be CRAM, BAM, SAM, or FASTQ)" % filetype)
        filetype = filetype.strip().upper()
        if not isinstance(reads_fn, str) and filetype != 'FASTQ':
            raise ValueError("Can only provide multiple reads files for FASTQ")
        if not isinstance(threads, int) or threads < 1:
            raise ValueError("Invalid number of threads: %s" % threads)

        # prep samtools and minimap2 commands
        command_samtools_view_cram = BASE_COMMAND_SAMTOOLS_VIEW_CRAM + ['-T', self.ref_f.name, '-@', str(threads)]
        if not include_unmapped:
            command_samtools_view_cram += ['-F', '4'] # only include mapped reads
        command_minimap2 = BASE_COMMAND_MINIMAP2 + ['-a', self.mmi_f.name]

        # handle CRAM (just read all data)
        if filetype == 'CRAM':
            f = open(reads_fn, 'rb', buffering=bufsize); cram_data = f.read(); f.close()

        # handle BAM/SAM (convert to CRAM)
        elif filetype == 'BAM' or filetype == 'SAM':
            cram_data = check_output(command_samtools_view_cram + [reads_fn])

        # handle FASTQ (map to ref + convert to CRAM)
        elif filetype == 'FASTQ':
            if isinstance(reads_fn, str):
                command_minimap2 += [reads_fn]
            else:
                command_minimap2 += reads_fn
            p_minimap2 = Popen(command_minimap2, stdout=PIPE, stderr=DEVNULL)
            cram_data = check_output(command_samtools_view_cram, stdin=p_minimap2.stdout)
            p_minimap2.wait()

        # invalid filetype
        else:
            raise TypeError("Invalid filetype: %s (must be CRAM, BAM, or SAM)" % filetype)

        # add this CRAM to the database
        curr_row = (ID, cram_data, None, None, None)
        self.cur.execute("INSERT INTO seqs VALUES(?, ?, ?, ?, ?)", curr_row)
        if commit:
            self.commit()

    def del_entry(self, ID, commit=True):
        '''Remove an entry to this database

        Args:
            ``ID`` (``str``): The unique ID of the entry to remove

            ``commit`` (``bool``): Commit database after removing this entry
        '''
        if self.cur.execute("SELECT ID FROM seqs WHERE ID='%s'" % ID).fetchone() is None:
            raise KeyError("ID doesn't exist in database: %s" % ID)
        self.cur.execute("DELETE FROM seqs WHERE ID='%s'" % ID)
        if commit:
            self.commit()

    def get_entry(self, ID):
        '''Return the data of an entry associated with a given ID in this database

        Args:
            ``ID`` (``str``): The unique ID of the entry to retrieve

        Returns:
            ``bytes`` object containing the CRAM data of the reads
            
            ``numpy.array`` object containing the position counts
            
            ``dict`` object containing the insertion counts
            
            ``str`` object containing the consensus sequence
        '''
        pass # 'CRAM', 'POS_COUNTS_XZ', 'INS_COUNTS_XZ', 'CONSENSUS_XZ
        tmp = self.cur.execute("SELECT CRAM, POS_COUNTS_XZ, INS_COUNTS_XZ, CONSENSUS_XZ FROM seqs WHERE ID='%s'" % ID).fetchone()
        if tmp is None:
            raise KeyError("ID doesn't exist in database: %s" % ID)
        cram, pos_counts_xz, ins_counts_xz, consensus_xz = tmp
        return cram, decompress_pos_counts(pos_counts_xz), decompress_ins_counts(ins_counts_xz), decompress_seq(consensus_xz)

    def compute_counts(self, ID, min_qual=DEFAULT_MIN_QUAL, bufsize=DEFAULT_BUFSIZE, overwrite=False, commit=True):
        '''Compute position and insertion counts for a given entry

        Args:
            ``ID`` (``str``): The unique ID of the entry whose counts to compute

            ``min_qual`` (``int``): Minimum base quality to count base

            ``bufsize`` (``int``): Buffer size for reading from file

            ``overwrite`` (``bool``): ``True`` to recompute (and overwrite) counts if they already exist

            ``commit`` (``bool``): Commit database after updating this entry
        '''
        # check for validity
        tmp = self.cur.execute("SELECT CRAM, POS_COUNTS_XZ, INS_COUNTS_XZ FROM seqs WHERE ID='%s'" % ID).fetchone()
        if tmp is None:
            raise KeyError("ID doesn't exist in database: %s" % ID)
        cram_data, pos_counts_xz, ins_counts_xz = tmp
        if pos_counts_xz is not None and ins_counts_xz is not None and not overwrite:
            raise ValueError("Counts already exist for ID: %s" % ID)

        # pull CRAM and compute counts
        cram_f = NamedTemporaryFile('wb', prefix='vireadb', suffix='.cram', buffering=bufsize)
        cram_f.write(cram_data); cram_f.flush(); aln = open_aln(cram_f.name, self.ref_f.name, threads=1)
        pos_counts, ins_counts = compute_base_counts(aln, self.ref_len, min_qual=min_qual)
        cram_f.close()

        # compress and save counts
        pos_counts_xz = compress_pos_counts(pos_counts)
        ins_counts_xz = compress_ins_counts(ins_counts)
        self.cur.execute("UPDATE seqs SET POS_COUNTS_XZ=? WHERE ID=?", (pos_counts_xz, ID))
        self.cur.execute("UPDATE seqs SET INS_COUNTS_XZ=? WHERE ID=?", (ins_counts_xz, ID))
        if commit:
            self.commit()

    def get_counts(self, ID):
        '''Return the position and insertion counts for a given entry

        Args:
            ``ID`` (``str``): The unique ID of the entry whose counts to return

        Returns:
            The position counts for ``ID`` (or ``None`` if not yet computed)

            The insertion counts for ``ID`` (or ``None`` if not yet computed)
        '''
        tmp = self.cur.execute("SELECT POS_COUNTS_XZ, INS_COUNTS_XZ FROM seqs WHERE ID='%s'" % ID).fetchone()
        if tmp is None:
            raise KeyError("ID doesn't exist in database: %s" % ID)
        pos_counts_xz, ins_counts_xz = tmp
        pos_counts = decompress_pos_counts(pos_counts_xz)
        ins_counts = decompress_ins_counts(ins_counts_xz)
        return pos_counts, ins_counts

    def compute_consensus(self, ID, min_depth=DEFAULT_MIN_DEPTH, min_freq=DEFAULT_MIN_FREQ, ambig=DEFAULT_AMBIG, remove_gaps=True, overwrite=False, commit=True):
        '''Compute the consensus sequence for a given entry. The position and insertion counts must have already been computed

        Args:
            ``ID`` (``str``): The unique ID of the entry whose counts to compute

            ``min_depth`` (``int``): Minimum depth to call base/insertion in consensus

            ``min_freq`` (``float``): Minimum frequency [0,1] to call base/insertion in consensus

            ``ambig`` (``str``): Symbol to use for ambiguous bases in consensus

            ``remove_gaps`` (``bool``): Remove gap characters (``-``) from consensus

            ``overwrite`` (``bool``): ``True`` to recompute (and overwrite) counts if they already exist

            ``commit`` (``bool``): Commit database after updating this entry
        '''
        # check for validity
        tmp = self.cur.execute("SELECT POS_COUNTS_XZ, INS_COUNTS_XZ, CONSENSUS_XZ FROM seqs WHERE ID='%s'" % ID).fetchone()
        if tmp is None:
            raise KeyError("ID doesn't exist in database: %s" % ID)
        pos_counts_xz, ins_counts_xz, consensus_xz = tmp
        if pos_counts_xz is None or ins_counts_xz is None:
            raise RuntimeError("Must compute counts before computing consensus for ID: %s" % ID)
        if consensus_xz is not None and not overwrite:
            raise ValueError("Consensus already exists for ID: %s" % ID)

        # decompress counts, compute consensus, and save
        pos_counts = decompress_pos_counts(pos_counts_xz)
        ins_counts = decompress_ins_counts(ins_counts_xz)
        consensus = compute_consensus(pos_counts, ins_counts, min_depth=min_depth, min_freq=min_freq, ambig=ambig)
        consensus_xz = compress_seq(consensus)
        self.cur.execute("UPDATE seqs SET CONSENSUS_XZ=? WHERE ID=?", (consensus_xz, ID))
        if commit:
            self.commit()

    def get_consensus(self, ID):
        '''Return the consensus sequence for a given entry

        Args:
            ``ID`` (``str``): The unique ID of the entry whose counts to return

        Returns:
            The consensus sequence for ``ID`` (or ``None`` if not yet computed)
        '''
        tmp = self.cur.execute("SELECT CONSENSUS_XZ FROM seqs WHERE ID='%s'" % ID).fetchone()
        if tmp is None:
            raise KeyError("ID doesn't exist in database: %s" % ID)
        return decompress_seq(tmp[0])

    def export_fasta(self, out_fn, IDs, overwrite=False):
        '''Export multiple consensus sequences as a FASTA file

        Args:
            ``out_fn`` (``str``): The path of the output FASTA file

            ``IDs`` (``list``): List of IDs whose consensus sequences to export

            ``overwrite`` (``bool``): Overwrite output file if it exists
        '''
        if isfile(out_fn) and not overwrite:
            raise ValueError("Output file exists: %s" % out_fn)
        if isinstance(IDs, str):
            IDs = [IDs]
        f = open(out_fn, 'w')
        for ID in IDs:
            try:
                seq = self.get_consensus(ID)
            except KeyError:
                warn("ID doesn't exist in database and was thus skipped: %s" % ID)
            if seq is None:
                warn("Consensus sequence hasn't been computed and was thus skipped: %s" % ID)
            else:
                f.write(">%s (vireadb v%s)\n%s\n" % (ID, self.version, seq))
        f.close()

def create_db(db_fn, ref_fn, overwrite=False, bufsize=DEFAULT_BUFSIZE):
    '''Create a new ViReaDB database

    Args:
        ``db_fn`` (``str``): The filename of the SQLite3 database file representing this database

        ``ref_fn`` (``str``): The filename of the viral reference genome to use for this database

        ``overwrite`` (``bool``): Overwrite ``db_fn`` if it already exists

    Returns:
        ``ViReaDB`` object
    '''
    # check valid inputs
    if not isfile(ref_fn):
        raise ValueError("File not found: %s" % ref_fn)
    if isdir(db_fn):
        raise ValueError("db_fn exists as a directory: %s" % db_fn)
    if isfile(db_fn):
        if overwrite:
            remove(db_fn)
        else:
            raise ValueError("db_fn exists: %s" % db_fn)

    # prep for any compression
    lzma_comp = LZMACompressor(preset=PRESET_EXTREME)

    # load reference genome
    ref_name, ref_seq = load_ref(ref_fn)
    ref_seq_xz = compress_seq(ref_seq)

    # index reference genome
    mmi_f = NamedTemporaryFile('w', prefix='vireadb', suffix='.mmi', buffering=bufsize)
    mmi_fn = mmi_f.name; mmi_f.close()
    call(BASE_COMMAND_MINIMAP2 + ['-d', mmi_fn, ref_fn], stdout=DEVNULL, stderr=DEVNULL)
    mmi_f = open(mmi_fn, 'rb'); mmi_data = mmi_f.read()
    mmi_f.close(); remove(mmi_fn)

    # create SQLite3 database and populate with `meta` table
    con = sqlite3.connect(db_fn); cur = con.cursor()
    cur.execute("CREATE TABLE meta(%s)" % ', '.join(META_TABLE_COLS))
    cur.execute("INSERT INTO meta VALUES(?, ?)", ('VERSION', VERSION))
    cur.execute("INSERT INTO meta VALUES(?, ?)", ('REF_NAME', ref_name))
    cur.execute("INSERT INTO meta VALUES(?, ?)", ('REF_SEQ_XZ', ref_seq_xz))
    cur.execute("INSERT INTO meta VALUES(?, ?)", ('REF_MMI', mmi_data))
    cur.execute("CREATE TABLE seqs(%s)" % ', '.join(SEQS_TABLE_COLS))
    con.commit(); con.close()
    return ViReaDB(db_fn)

def load_db(db_fn):
    '''Load a ViReaDB database from file

    Args:
        ``db_fn`` (``str``): The filename of the SQLite3 database file representing this database

    Returns:
        ``ViReaDB`` object
    '''
    if not isfile(db_fn):
        raise ValueError("db_fn not found: %s" % db_fn)
    return ViReaDB(db_fn)
