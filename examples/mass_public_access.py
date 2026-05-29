#!/usr/bin/env python3
"""
Put all media described by oids in a CSV file (first column) as public

"""
import argparse
import os
import sys

if __name__ == '__main__':
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from nudgisclient import NudgisClient

    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument(
        '--conf',
        help='Path to the configuration file.',
        required=True,
        type=str
    )

    parser.add_argument(
        '--csv',
        help='Path to CSV file; the column expceted to be the OID \
                is defined by the --column option. Lines starting with "#" will be ignored',
        required=True,
        type=str
    )

    parser.add_argument(
        '--column',
        help='Column count where the oid should be expected',
        default=0,
        type=int
    )

    parser.add_argument(
        '--csv-separator',
        help='CSV separator',
        default='\t',
        type=str
    )

    args = parser.parse_args()

    ngc = NudgisClient(args.conf)
    # ping
    print(ngc.api('/'))

    with open(args.csv, 'r') as f:
        csv_data = f.read().strip()
        count = 0
        freed = 0
        lines = [line for line in csv_data.split('\n') if (line and not line.startswith('#'))]
        total_lines = len(lines)
        print(f'About to make {total_lines} media public')
        # there is a limit to how many subprocesses can be launched

        for index, line in enumerate(lines):
            oid = line.split(args.csv_separator)[args.column]
            if oid:
                params = {'oid': oid, 'full': 'yes'}
                try:
                    print(f'[{index + 1}/{total_lines}] About to set {oid} public')
                    data = {
                        'oid': oid,
                        'validated': 'yes'
                    }
                    print(f'Validating {oid}')
                    ngc.api('medias/edit/', method='post', data=data)
                    data = {
                        'oid': oid,
                        'users-anonymous-can_access_media': 'True',
                        'users-authenticated-can_access_media': 'True',
                        'prefix': 'reference',
                    }
                    print(f'Making {oid} public')
                    ngc.api('perms/edit/default/', method='post', data=data)
                    count += 1
                except Exception as e:
                    print(f'Error on {oid}: {e}')
        print(f'Made {count} media public')
