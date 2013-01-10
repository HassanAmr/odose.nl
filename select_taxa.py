#!/usr/bin/env python
"""Module for the select taxa step."""

from collections import defaultdict
from csv import DictReader
from datetime import datetime, timedelta
from divergence import create_directory, parse_options
from divergence.download_taxa_mrs import download_genome_files
from ftplib import FTP
from operator import itemgetter
import logging as log
import os
import sys
import time

__author__ = "Tim te Beek"
__contact__ = "brs@nbic.nl"
__copyright__ = "Copyright 2011, Netherlands Bioinformatics Centre"
__license__ = "MIT"


def select_genomes_by_ids(genome_ids):
    """Return list of genomes from complete genomes table whose GenBank Project ID is in genome_ids."""
    #Loop over genomes and return any genomes whose GenBank Project ID is in genome_ids
    refseq_genomes = dict((genome['BioProject ID'], genome) for genome in _parse_genomes_table())

    #Match genomes_ids to genomes
    matches = dict((queryid, refseq_genomes[queryid]) for queryid in genome_ids if queryid in refseq_genomes)

    #See if we matched all genomes, else log a warning
    for queryid in genome_ids:
        if queryid not in matches:
            log.warning('Could not find genome with BioProject ID %s in complete genomes table', queryid)

    return matches


def _download_genomes_table():
    '''Dowload the prokaryotes.txt genome table file from the NCBI FTP site, save a local copy and return contents.'''
    cache_dir = create_directory('')
    prokaryotes = 'prokaryotes.txt'
    output_file = os.path.join(cache_dir, prokaryotes)

    #Only download when existing file is older than a day
    time_between_downloads = 24 * 60 * 60
    if not os.path.isfile(output_file) or os.path.getmtime(output_file) < time.time() - time_between_downloads:
        #Login to FTP site
        ftp = FTP('ftp.ncbi.nlm.nih.gov')
        ftp.login(passwd='brs@nbic.nl')

        #Download ftp://ftp.ncbi.nlm.nih.gov/genomes/GENOME_REPORTS/prokaryotes.txt
        from download_taxa_ncbi import _download_genome_file
        _download_genome_file(ftp, '/genomes/GENOME_REPORTS', prokaryotes, cache_dir, datetime.now())

    #Read file and return content
    with open(output_file) as read_handle:
        return read_handle.read()


def _parse_genomes_table(require_refseq=False):
    """Parse table of genomes and return list of dictionaries with values per genome."""
    #Empty lists to hold column names and genome dictionaries
    genomes = []

    #We expect the following columns:
    #Organism/Name    BioProject    Group    SubGroup    Size (Mb)    GC%    Chromosomes/RefSeq    Chromosomes/INSDC
    #Plasmids/RefSeq    Plasmids/INSDC    WGS    Scaffolds    Genes    Proteins    Release Date    Modify Date
    #Status    Center

    #Split file into individual lines
    contents = _parse_genomes_table.complete_genome_table
    assert contents.startswith('#Organism/Name\t'), 'Unexpected file format:\n' + contents

    #Some columns contain lists of values separated by a delimiter themselves: We'll split their values accordingly
    splitable_columns = {'Chromosomes/RefSeq': ',',
                         'Chromosomes/INSDC': ',',
                         'Plasmids/RefSeq': ',',
                         'Plasmids/INSDC': ',',
                         #'Center': '; ',
                         }

    #Convert string file contents to something readable by CSV DictReader
    from StringIO import StringIO
    filelike = StringIO(contents[1:])

    #Loop over dictionaries generated by CSV DictReader
    for genome in DictReader(filelike, delimiter='\t'):
        #Split values based on separator mapping for column name in splitable_columns
        for column, separator in splitable_columns.iteritems():
            value = genome[column]
            genome[column] = [] if len(value) in (0, 1) else value.split(separator)

        #Convert date columns to actual dates
        #Released date 2009/01/27
        if genome['Release Date'] != '-':
            genome['Release Date'] = datetime.strptime(genome['Release Date'], '%Y/%m/%d')
        else:
            genome['Release Date'] = None
        #Modified date 2011/02/10
        if genome['Modify Date'] != '-':
            genome['Modify Date'] = datetime.strptime(genome['Modify Date'], '%Y/%m/%d')
        else:
            genome['Modify Date'] = None

        #Append genome to list of genomes
        genomes.append(genome)

    #Filter out records not containing a refseq entry
    if require_refseq:
        genomes = [genome for genome in genomes if genome['Chromosomes/RefSeq']]

    #Filter out all genomes that do not have any chromosomes
    genomes = [genome for genome in genomes if genome['Chromosomes/RefSeq'] or genome['Chromosomes/INSDC']]

    #Return the genome dictionaries
    return tuple(genomes)

#Assign content returned from _download_genomes_table as default value for complete_genome_table, such that we can
#override this value in tests
_parse_genomes_table.complete_genome_table = _download_genomes_table()


def _bin_using_keyfunctions(genomes, attributes=('Group', 'SubGroup', 'Organism/Name')):
    """Bin genomes recursively according to attributes, returning nested dictionaries mapping keys to collections."""

    #Get first keyfunction, and bin genomes by that keyfunction
    bins = defaultdict(list)
    #Loop over genomes to map each to the right group
    for genome in genomes:
        #Determine key from keyfunction
        key = itemgetter(attributes[0])(genome)
        #Add this genome to this colllection
        bins[key].append(genome)

    #If there are further keyfunctions available, recursively bin groups identified by current bin function
    if 1 < len(attributes):
        for key, subset in bins.iteritems():
            bins[key] = _bin_using_keyfunctions(subset, attributes[1:])

    return [genome for key in sorted(bins.keys()) for genome in bins[key]]


def get_complete_genomes(genomes=_parse_genomes_table()):
    """Get tuples of Organism Name, GenBank Project ID & False, for input into Galaxy clade selection."""
    #Bin genomes using the following key functions iteratively
    sorted_genomes = _bin_using_keyfunctions(genomes)

    for genome in sorted_genomes:
        name = '{project} - {group} > {subgroup} > {firstname} > {fullname}'.format(
            project=genome['BioProject ID'],
            group=genome['Group'],
            subgroup=genome['SubGroup'],
            firstname=genome['Organism/Name'].split()[0],
            fullname=genome['Organism/Name'])
        labels = _get_colored_labels(genome)
        if labels:
            name += ' - ' + labels

        #Yield the composed name, the project ID & False according to the expected input for Galaxy
        yield name, genome['BioProject ID'], False


def _get_colored_labels(genome, html_is_escaped=True):
    """Optionally return colored labels for genome based on a release date, modified date and genome size."""
    labels = ''

    #Add New! & Updated! labels by looking at release and updated dates in the genome dictionary
    day_limit = datetime.today() - timedelta(days=30)

    #Released date 01/27/2009
    released_date = genome['Release Date']
    if released_date and day_limit < released_date:
        since = 'Since {0}'.format(released_date.strftime('%b %d'))
        if html_is_escaped:
            labels += '*' + since + '*'
        else:
            labels += '<span title="{0}" style="background-color: lightgreen">New!</span>'.format(since)

    #Modified date 02/10/2011
    modified_date = genome['Modify Date']
    if not labels and modified_date and day_limit < modified_date:
        updated = 'Updated {0}'.format(modified_date.strftime('%b %d'))
        if html_is_escaped:
            labels += '*' + updated + '*'
        else:
            labels += '<span title="{0}" style="background-color: yellow">Updated!</span>'.format(updated)

    #Warn when genomes contain less than 0.5 MegaBase: unlikely to result in any orthologs
    genome_size = genome['Size (Mb)']
    if genome_size and genome_size != '-' and float(genome_size) < 1:
        if html_is_escaped:
            labels += '*Only {0} Mb!*'.format(genome_size)
        else:
            ttl = 'title="Small genomes are unlikely to result in orthologs present across all genomes"'
            style = 'style="background-color: orange"'
            labels += '<span {0} {1}>Only {2} Mb!</span>'.format(ttl, style, genome_size)

    return labels


def main(args):
    """Main function called when run from command line or as part of pipeline."""
    usage = """
Usage: select_taxa.py
--genomes=ID,...           optional comma-separated list of selected GenBank Project IDs from complete genomes table
--previous-file=FILE       optional previously or externally created GenBank Project IDs file whose genomes should be reselected
--require-protein-table    require protein table files to be present for all downloaded genomes
--genomes-file=FILE        destination path for file with selected genome IDs followed by Organism Name on each line
"""
    options = ['genomes=?', 'previous-file=?', 'require-protein-table?', 'genomes-file']
    genomes_line, previous_file, require_ptt, genomes_file = parse_options(usage, options, args)

    #Genome IDs selected by the user that refer to GenBank or RefSeq entries
    genome_ids = []

    #Split ids on comma
    if genomes_line:
        genome_ids.extend(val for val in genomes_line.split(',') if val)

    #Allow for input of previous or externally created genomes-file to rerun an analysis
    if previous_file:
        #Read previous GenBank Project IDs from previous_file, each on their own line
        with open(previous_file) as read_handle:
            genome_ids.extend(line.split()[0] for line in read_handle
                              #But skip external genomes as their IDs will fail to download
                              if 'external genome' not in line)

    #Assert each clade contains enough IDs
    maximum = 100
    #TODO Move this test to translate, where we can see how many translations succeeded + how many externals there are
    if  maximum < len(genome_ids):
        log.error('Expected between two and {0} selected genomes, but was {1}'.format(maximum, len(genome_ids)))
        sys.exit(1)

    #Retrieve genome dictionaries to get to Organism Name
    genomes = select_genomes_by_ids(genome_ids).values()
    genomes = sorted(genomes, key=itemgetter('Organism/Name'))

    #Semi-touch genomes file in case no genomes were selected, for instance when uploading external genomes
    open(genomes_file, mode='a').close()

    #Write IDs to file, with organism name as second column to make the project ID files more self explanatory.
    for genome in genomes:
        #Download files here, but ignore returned files: These can be retrieved from cache during extraction/translation
        download_genome_files(genome, genomes_file, require_ptt=require_ptt)

    # Post check after translation to see if more than one genome actually had some genomic contents
    with open(genomes_file) as read_handle:
        genome_ids = [line.split()[0] for line in read_handle]
        # If some genomes were skipped, ensure at least two genomes remain
        if len([gid for gid in genome_ids if gid.startswith('#')]):
            assert 2 <= len([gid for gid in genome_ids if not gid.startswith('#')]), \
                "Some genomes were skipped, leaving us with less than two genomes to operate on; " \
                "Inspect messages in Project ID list and reevaluate genome selection"

    #Exit after a comforting log message
    log.info("Produced: \n%s", genomes_file)

if __name__ == '__main__':
    main(sys.argv[1:])
