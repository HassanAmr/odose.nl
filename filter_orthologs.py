#!/usr/bin/env python
"""Module to clean up orthologs after OrthoMCL step."""

from __future__ import division
from Bio import AlignIO, SeqIO
from Bio.SeqRecord import SeqRecord
from divergence import create_directory, extract_archive_of_files, create_archive_of_files, parse_options
from multiprocessing import Pool
from operator import itemgetter
from subprocess import check_call, STDOUT
import logging as log
import os
import shutil
import sys
import tempfile

def _cog_based_filtering(sico_files):
    """Inspect COGs for sequences marked as orthologs by OrthoMCL, and append some details about this to stats_file."""
    #Retrieve SICO to cog dictionaries of cog conflicts & transferable cog annotations, and list of SICOs missing cog
    cog_conflicts, cog_transferable, cog_missing = _group_cog_issues(sico_files)

    #Filter out orthologs containing more than one COG annotation
    sico_files = [sico for sico in sico_files if sico not in cog_conflicts.keys()]

    #Transfer COGs by overwriting sico_files with correct COG set
    for sico_file, cog in cog_transferable.iteritems():
        seqrecords = SeqIO.to_dict(SeqIO.parse(sico_file, 'fasta')).values()
        with open(sico_file, mode = 'w') as write_handle:
            for seqr in seqrecords:
                #Sample header line: >58191|NC_010067.1|YP_001569097.1|COG4948MR|core
                #Or for missing COG: >58191|NC_010067.1|YP_001569097.1|None|core
                split = seqr.id.split('|')
                assert split[3] in (cog, 'None'), 'COG should be either {0} or None, but was {1}'.format(cog, split[3])
                split[3] = cog
                seqr = SeqRecord(seqr.seq, id = '|'.join(split), description = '')
                SeqIO.write(seqr, write_handle, 'fasta')

    #Log COG statistics
    _log_cog_statistics(cog_conflicts, cog_transferable, cog_missing)

    return sico_files

def _group_cog_issues(sico_files):
    """Find issues with COG assignments within SICO files by looking at COG conflicts, transferable and missing COGs."""
    cog_conflicts = {}
    cog_transferable = {}
    cog_missing = []
    for sico_file in sico_files:
        with open(sico_file) as read_handle:
            cogs = set()
            unassigned_cog_found = False
            for record in SeqIO.parse(read_handle, 'fasta'):
                #Sample header line: >58191|NC_010067.1|YP_001569097.1|COG4948MR|core
                cog = record.id.split('|')[3]
                if cog in cogs:
                    continue
                if cog == 'None':
                    unassigned_cog_found = True
                    continue
                cogs.add(cog)
            if 0 == len(cogs):
                cog_missing.append(sico_file)
            elif 1 == len(cogs):
                if unassigned_cog_found:
                    cog_transferable[sico_file] = cogs.pop()
            elif 1 < len(cogs):
                cog_conflicts[sico_file] = cogs
    return cog_conflicts, cog_transferable, cog_missing


def _log_cog_statistics(cog_conflicts, cog_transferable, cog_missing):
    """Append COG statistics to stats_file"""
    if cog_conflicts:
        log.info('Multiple COGs found in {0} SICOs:'.format(len(cog_conflicts)))
        for sico_file in sorted(cog_conflicts.keys()):
            cogs = cog_conflicts[sico_file]
            log.info('{0}:\t{1}'.format(os.path.split(sico_file)[1], '\t'.join(cogs)))
    if cog_transferable:
        log.info('COGs transfered in {0} SICOs:'.format(len(cog_transferable)))
        for sico_file in sorted(cog_transferable.keys()):
            cog = cog_transferable[sico_file]
            log.info('{0}:\t{1}'.format(os.path.split(sico_file)[1], cog))
    if cog_missing:
        log.info('No COGs found in {0} SICOs:'.format(len(cog_missing)))
        for sico_file in cog_missing:
            log.info(os.path.split(sico_file)[1])

def _align_sicos(run_dir, sico_files):
    """Align all SICO files given as argument in parallel and return the resulting alignment files."""
    log.info('Aligning {0} SICO genes using TranslatorX & muscle.'.format(len(sico_files)))
    #We'll multiplex this embarrassingly parallel task using a pool of workers
    tuples = [(run_dir, sico_file) for sico_file in sico_files]
    return Pool().map(_run_translatorx, tuples)

TRANSLATORX = '/projects/divergence/software/translatorx/translatorx_v1.1.pl'

def _run_translatorx((run_dir, sico_file), translation_table = '11'):
    """Run TranslatorX to create DNA level alignment file of protein level aligned DNA sequences within sico_file."""
    assert os.path.exists(TRANSLATORX) and os.access(TRANSLATORX, os.X_OK), 'Could not find or run ' + TRANSLATORX

    #Determine output file name
    sico_base = os.path.splitext(os.path.split(sico_file)[1])[0]
    alignment_dir = create_directory('alignments/' + sico_base, inside_dir = run_dir)

    #Created output file
    file_base = os.path.join(alignment_dir, sico_base)
    dna_alignment = file_base + '.nt_ali.fasta'

    #Actually run the TranslatorX program
    command = [TRANSLATORX,
               '-i', sico_file,
               '-c', translation_table,
               '-o', file_base]
    check_call(command, stdout = open('/dev/null', 'w'), stderr = STDOUT)

    msg = 'Alignment file should exist and have some content now: {0}'.format(dna_alignment)
    assert os.path.isfile(dna_alignment) and 0 < os.path.getsize(dna_alignment), msg
    return dna_alignment

def _trim_alignments(run_dir, dna_alignments, retained_threshold, stats_file):
    """Trim all DNA alignments using _trim_alignment (singular), and calculate some statistics about the trimming."""
    log.info('Trimming {0} DNA alignments from first non-gap codon to last non-gap codon'.format(len(dna_alignments)))

    #Create directory here, to prevent race-condition when folder does not exist, but is then created by another process
    trimmed_dir = create_directory('trimmed', inside_dir = run_dir)

    #Use Pool().map again to scale trimming out over multiple cores. This requires tuple'd arguments however
    tuples = [(trimmed_dir, dna_alignment) for dna_alignment in dna_alignments]
    trim_tpls = Pool().map(_trim_alignment, tuples)

    remaining_percts = [tpl[3] for tpl in trim_tpls]
    trimmed_alignments = [tpl[0] for tpl in trim_tpls if retained_threshold <= tpl[3]]

    #Write trim statistics to file in such a way that they're easily converted to a graph in Galaxy
    with open(stats_file, mode = 'w') as append_handle:
        msg = '{0:6} sequence alignments trimmed'.format(len(trim_tpls))
        log.info(msg)
        append_handle.write('#' + msg + '\n')

        average_retained = sum(remaining_percts) / len(remaining_percts)
        msg = '{0:5.2}% sequence retained on average overall'.format(average_retained)
        log.info(msg)
        append_handle.write('#' + msg + '\n')

        filtered = len(trim_tpls) - len(trimmed_alignments)
        msg = '{0:6} orthologs filtered as they retained less than {1}%'.format(filtered, str(retained_threshold))
        log.info(msg)
        append_handle.write('#' + msg + '\n')

        append_handle.write('# Trimmed file\tOriginal length\tTrimmed length\tPercentage retained\n')
        for tpl in sorted(trim_tpls, key = itemgetter(3)):
            append_handle.write(os.path.split(tpl[0])[1] + '\t')
            append_handle.write(str(tpl[1]) + '\t')
            append_handle.write(str(tpl[2]) + '\t')
            append_handle.write('{0:.2f}\n'.format(tpl[3]))

    return sorted(trimmed_alignments)

def _trim_alignment((trimmed_dir, dna_alignment)):
    """Trim alignment to retain first & last non-gapped codons across alignment, and everything in between (+gaps!).
    
    Return trimmed file, original length, trimmed length and percentage retained as tuple"""
    #Read single alignment from fasta file
    alignment = AlignIO.read(dna_alignment, 'fasta')
    #print '\n'.join([str(seqr.seq) for seqr in alignment])

    #Total alignment should be just as long as first sequence of alignment
    alignment_length = len (alignment[0])

    #After using protein alignment only for CDS, all alignment lengths should be multiples of three 
    assert alignment_length % 3 == 0, 'Length not a multiple of three: {} \n{2}'.format(alignment_length, alignment)

    #Assert all codons are either full length codons or gaps, but not a mix of gaps and letters such as AA- or A--
    for index in range(0, alignment_length, 3):
        for ali in alignment:
            codon = ali.seq[index:index + 3]
            assert not ('-' in codon and str(codon) != '---'), '{0} at {1} in \n{2}'.format(codon, index, alignment)

    #Loop over alignment, taking 3 DNA characters each time, representing a single codon
    first_full_codon_start = None
    last_full_codon_end = None
    for index in range(0, alignment_length, 3):
        codon_concatemer = ''.join([str(seqr.seq) for seqr in alignment[:, index:index + 3]])
        if '-' in codon_concatemer:
            continue
        if first_full_codon_start is None:
            first_full_codon_start = index
        else:
            last_full_codon_end = index + 3

    #Create sub alignment consisting of all trimmed sequences from full alignment
    trimmed = alignment[:, first_full_codon_start:last_full_codon_end]
    trimmed_length = len(trimmed[0])
    assert trimmed_length % 3 == 0, 'Length not a multiple of three: {} \n{2}'.format(trimmed_length, trimmed)

    #Write out trimmed alignment file
    trimmed_file = os.path.join(trimmed_dir, os.path.split(dna_alignment)[1])
    with open(trimmed_file, mode = 'w') as write_handle:
        AlignIO.write(trimmed, write_handle, 'fasta')

    #Assert file now exists with content
    assert os.path.isfile(trimmed_file) and os.path.getsize(trimmed_file), \
        'Expected trimmed alignment file to exist with some content now: {0}'.format(trimmed_file)

    return trimmed_file, alignment_length, trimmed_length, trimmed_length / alignment_length * 100

def _concatemer_per_genome(run_dir, genome_ids, trimmed_sicos):
    """Create a concatemer DNA file per genome containing all aligned & trimmed SICO genes."""
    concatemer_dir = create_directory('concatemers', inside_dir = run_dir)
    log.info('Creating {0} concatemers from {1} SICOs'.format(len(genome_ids), len(trimmed_sicos)))

    #Open trimmed concatemer write handles
    concatemer_files = []
    write_handles = {}

    #For each genome, open a file for the trimmed SICO genes concatemer
    for refseq_id in genome_ids:
        #Build up output file path
        concatemer_file = os.path.join(concatemer_dir, refseq_id + '.trimmed.concatemer.fasta')
        concatemer_files.append(concatemer_file)

        #Open write handle
        write_handle = open(concatemer_file, mode = 'w')
        write_handles[refseq_id] = write_handle

        #Write initial fasta header
        write_handle.write('> {0}|trimmed concatemer\n'.format(refseq_id))

    #Loop over trimmed sico files to append each sequence to the right concatemer
    for trimmed_sico in trimmed_sicos:
        with open(trimmed_sico) as read_handle:
            for seqr in SeqIO.parse(read_handle, 'fasta'):
                #Sample header line: >58191|NC_010067.1|YP_001569097.1|COG4948MR|core                
                refseq_id = seqr.id.split('|')[0]
                write_handles[refseq_id].write('{0}\n'.format(str(seqr.seq)))

    #Close genomes trimmed concatemer write handles 
    for write_handle in write_handles.values():
        write_handle.close()

    return concatemer_files

def main(args):
    """Main function called when run from command line or as part of pipeline."""
    usage = """
Usage: filter_orthologs.py
--genomes=FILE               file with refseq id from complete genomes table on each line 
--orthologs-zip=FILE         archive of orthologous genes in FASTA format

--filter-multiple-cogs       filter orthologs with multiple COG annotations among genes
--filter-recombination       filter orthologs that show recombination when comparing phylogenetic trees
--retained-threshold=PERC    filter orthologs that retain less than PERC % of sequence after trimming alignment 

--trimmed-zip=FILE           destination file path for archive of aligned & trimmed orthologous genes
--concatemer-zip=FILE        destination file path for archive of concatemers per genome
--stats=FILE                 destination file path for ortholog trimming statistics file
"""
    options = ['genomes', 'orthologs-zip', 'filter-multiple-cogs?', 'filter-recombination?', 'retained-threshold', \
               'trimmed-zip', 'concatemer-zip', 'stats']
    genome_ids_file, orthologs_zip, filter_cogs, filter_recombination, retained_threshold, \
    target_trimmed, target_concatemer, target_stats_path = parse_options(usage, options, args)

    #Convert retained threshold to integer, so we can fail fast if argument was passed incorrectly
    retained_threshold = int(retained_threshold)

    #Parse file containing RefSeq project IDs to extract RefSeq project IDs
    with open(genome_ids_file) as read_handle:
        genomes = [line.strip() for line in read_handle]

    #Run filtering in a temporary folder, to prevent interference from simultaneous runs
    run_dir = tempfile.mkdtemp(prefix = 'filter_run_')

    #Extract files from zip archive
    temp_dir = create_directory('orthologs', inside_dir = run_dir)
    sico_files = extract_archive_of_files(orthologs_zip, temp_dir)

    #Filter orthologs with multiple COG annotations among genes if flag was set
    if filter_cogs:
        #Look COG assignment among orthologs; filter those with multiple COGs & carry over COGs to unannotated sequences
        sico_files = _cog_based_filtering(sico_files)

    #Filter orthologs that show recombination when comparing phylogenetic trees if flag was set
    if filter_recombination:
        #TODO Implement filtering out orthologs with evidence of recombination through comparing phylogenetic trees
        pass

    #TODO Add option to filter out SICOs when any ortholog has been flagged as 'mobile element', 'phage' or 'IS element'

    #Filter orthologs that retain less than PERC % of sequence after trimming alignment
    trim_stats_file = os.path.join(run_dir, 'trim-stats.txt')
    aligned_files = _align_sicos(run_dir, sico_files)
    trimmed_files = _trim_alignments(run_dir, aligned_files, retained_threshold, trim_stats_file)

    #Concatenate trimmed_files per genome
    concatemer_files = _concatemer_per_genome(run_dir, genomes, trimmed_files)

    #Create archives of files on command line specified output paths & move trim_stats_file
    create_archive_of_files(target_trimmed, trimmed_files)
    create_archive_of_files(target_concatemer, concatemer_files)
    shutil.move(trim_stats_file, target_stats_path)

    #Remove unused files to free disk space 
    shutil.rmtree(run_dir)

    #Exit after a comforting log message
    log.info("Produced: \n%s\n%s\n%s", target_trimmed, target_concatemer, target_stats_path)
    return target_trimmed, target_concatemer, target_stats_path

if __name__ == '__main__':
    main(sys.argv[1:])