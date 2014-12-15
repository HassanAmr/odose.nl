#!/usr/bin/env python
"""Module for the select taxa step."""

import argparse
from collections import defaultdict
from csv import DictReader
from datetime import datetime, timedelta
from ftplib import FTP
import logging
from operator import itemgetter
import os
import re
import sys
import time

from shared import create_directory


__author__ = "Tim te Beek"
__contact__ = "brs@nbic.nl"
__copyright__ = "Copyright 2011, Netherlands Bioinformatics Centre"
__license__ = "MIT"


def _download_genomes_table():
    '''Dowload the prokaryotes.txt genome table file from the NCBI FTP site, save a local copy and return contents.'''
    cache_dir = create_directory('')
    prokaryotes = 'prokaryotes.txt'
    output_file = os.path.join(cache_dir, prokaryotes)

    # Only download when existing file is older than a day
    time_between_downloads = 24 * 60 * 60
    if not os.path.isfile(output_file) or os.path.getmtime(output_file) < time.time() - time_between_downloads:
        # Login to FTP site
        ftp = FTP('ftp.ncbi.nlm.nih.gov')
        ftp.login(passwd='timtebeek+odose@gmail.com')

        # Download ftp://ftp.ncbi.nlm.nih.gov/genomes/GENOME_REPORTS/prokaryotes.txt
        from download_taxa_ncbi import _download_genome_file
        _download_genome_file(ftp, '/genomes/GENOME_REPORTS', prokaryotes, cache_dir, datetime.now())

    # Read file and return content
    with open(output_file) as read_handle:
        return read_handle.read()


def _parse_genomes_table(require_refseq=False):
    """Parse table of genomes and return list of dictionaries with values per genome."""
    # Empty lists to hold column names and genome dictionaries
    genomes = []

    # We expect the following columns:
    # Organism/Name    TaxID    BioProject Accession    BioProject ID    Group    SubGroup  Size (Mb)    GC%
    # Chromosomes/RefSeq    Chromosomes/INSDC    Plasmids/RefSeq    Plasmids/INSDC    WGS    Scaffolds    Genes
    # Proteins    Release Date    Modify Date    Status    Center    BioSample Accession    Assembly Accession
    # Reference    FTP Path    Pubmed ID

    # Split file into individual lines
    contents = _parse_genomes_table.complete_genome_table
    assert contents.startswith('#Organism/Name\t'), 'Unexpected file format:\n' + contents.split('\n')[0]

    # Some columns contain lists of values separated by a delimiter themselves: We'll split their values accordingly
    splitable_columns = {'Chromosomes/RefSeq': ',',
                         'Chromosomes/INSDC': ',',
                         'Plasmids/RefSeq': ',',
                         'Plasmids/INSDC': ','
                         }

    # Convert string file contents to something readable by CSV DictReader
    from StringIO import StringIO
    filelike = StringIO(contents[1:])

    # Loop over dictionaries generated by CSV DictReader
    for genome in DictReader(filelike, delimiter='\t'):
        # Split values based on separator mapping for column name in splitable_columns
        for column, separator in splitable_columns.iteritems():
            value = genome[column]
            genome[column] = [] if len(value) in (0, 1) else value.split(separator)

        # Any gaps might influence the core gene set
        if genome['Status'] != 'Complete Genome':
            continue

        # Skip any genomes that don't point to chromosome files
        if not genome['Chromosomes/RefSeq']:
            logging.debug('Missing Chromosomes/RefSeq identifiers for: %s', genome)
            continue

        # Skip any genomes that do not provide an FTP path to download
        if genome['FTP Path'] == '-':
            logging.debug('Missing FTP path for: %s', genome)
            continue

        # Convert date columns to actual dates
        # Released date 2009/01/27
        if genome['Release Date'] != '-':
            genome['Release Date'] = datetime.strptime(genome['Release Date'], '%Y/%m/%d')
        else:
            genome['Release Date'] = None
        # Modified date 2011/02/10
        if genome['Modify Date'] != '-':
            genome['Modify Date'] = datetime.strptime(genome['Modify Date'], '%Y/%m/%d')
        else:
            genome['Modify Date'] = None

        # Trim unique Assembly Accession to shorter values for older tools
        genome['Assembly Accession'] = re.sub('^GCA_0*', '', genome['Assembly Accession'])

        # Append genome to list of genomes
        genomes.append(genome)

    logging.debug('%d genomes initially', len(genomes))

    # Filter out records not containing a refseq entry
    if require_refseq:
        genomes = [genome for genome in genomes if genome['Chromosomes/RefSeq']]
        logging.debug('%d genomes have refseq identifiers', len(genomes))

    # Filter out genomes without any genes or Proteins
    genomes = [genome for genome in genomes if genome['Genes'] != '-' and genome['Proteins'] != '-']
    logging.debug('%d genomes have genes and proteins', len(genomes))

    # Filter out genomes with less than 100 Proteins
    genomes = [genome for genome in genomes if int(genome['Proteins']) > 100]
    logging.debug('%d genomes have more than 100 proteins', len(genomes))

    # Return the genome dictionaries
    return tuple(genomes)

# Assign content returned from _download_genomes_table as default value for complete_genome_table, such that we can
# override this value in tests
_parse_genomes_table.complete_genome_table = _download_genomes_table()


def _bin_using_keyfunctions(genomes, attributes=('Group', 'SubGroup', 'Organism/Name')):
    """Bin genomes recursively according to attributes, returning nested dictionaries mapping keys to collections."""

    # Get first keyfunction, and bin genomes by that keyfunction
    bins = defaultdict(list)
    # Loop over genomes to map each to the right group
    for genome in genomes:
        # Determine key from keyfunction
        key = itemgetter(attributes[0])(genome)
        # Add this genome to this colllection
        bins[key].append(genome)

    # If there are further keyfunctions available, recursively bin groups identified by current bin function
    if 1 < len(attributes):
        for key, subset in bins.iteritems():
            bins[key] = _bin_using_keyfunctions(subset, attributes[1:])

    return [genome for key in sorted(bins.keys()) for genome in bins[key]]


def get_complete_genomes(genomes=_parse_genomes_table()):
    """Get tuples of Organism Name, GenBank Project ID & False, for input into Galaxy clade selection."""
    # Bin genomes using the following key functions iteratively
    sorted_genomes = _bin_using_keyfunctions(genomes)

    for genome in sorted_genomes:
        name = '{project} - {group} > {subgroup} > {firstname} > {fullname}'.format(
            project=genome['BioProject ID'],
            group=genome['Group'],
            subgroup=genome['SubGroup'],
            firstname=genome['Organism/Name'].split()[0],
            fullname=genome['Organism/Name'])
        labels = _get_labels(genome)
        if labels:
            name += ' - ' + labels

        # Yield the composed name and the project ID
        yield name, genome['BioProject ID']


def _get_labels(genome):
    """Optionally return colored labels for genome based on a release date, modified date and genome size."""
    labels = ''

    # Add New! & Updated! labels by looking at release and updated dates in the genome dictionary
    day_limit = datetime.today() - timedelta(days=30)

    # Released date 01/27/2009
    released_date = genome['Release Date']
    if released_date and day_limit < released_date:
        since = 'Since {0}'.format(released_date.strftime('%b %d'))
        labels += '++' + since + '++'

    # Modified date 02/10/2011
    modified_date = genome['Modify Date']
    if not labels and modified_date and day_limit < modified_date:
        updated = 'Updated {0}'.format(modified_date.strftime('%b %d'))
        labels += '**' + updated + '**'

    # Warn when genomes contain less than 0.5 MegaBase: unlikely to result in any orthologs
    genome_size = genome['Size (Mb)']
    if genome_size and genome_size != '-' and float(genome_size) < 1:
        labels += '~~Only {0} Mb!~~'.format(genome_size)

    return labels


def _parse_args():
    '''
    Parse required arguments.
    '''
    parser = argparse.ArgumentParser()
    parser.add_argument('target',
                        help='Target output file for table',
                        type=argparse.FileType('w'),
                        default=sys.stdout)
    return parser.parse_args()


def main():
    # Parse arguments
    args = _parse_args()

    # Write genomes to tabular file
    genomes = get_complete_genomes()
    for key, value in genomes:
        args.target.write('{}\t{}\n'.format(value, key))
    args.target.close()


if __name__ == '__main__':
    main()
