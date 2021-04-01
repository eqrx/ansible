#!/usr/bin/env python
# PYTHON_ARGCOMPLETE_OK

# (c) 2020 Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

"""
CLI tool that analyses a Shippable run's test result and re-balances the test targets into new groups.

Before running this script you must run download.py like:

    ./download.py https://app.shippable.com/github/<team>/<repo>/runs/<run_num> --test-results --job-number x --job-number y

Or to get all job results from a run:

    ./download.py https://app.shippable.com/github/<team>/<repo>/runs/<run_num> --test-results --all


Set the dir <team>/<repo>/<run_num> as the value of '-p/--test-path' for this script.
"""

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import datetime
import argparse
import json
import operator
import os
import re
import requests

from glob import glob

try:
    import argcomplete
except ImportError:
    argcomplete = None


def run_id_arg(arg):
    m = re.fullmatch(r"(?:https:\/\/dev\.azure\.com\/ansible\/ansible\/_build\/results\?buildId=)?(\d{4})", arg)
    if not m:
        raise ValueError("run does not seems to be a URI or an ID")
    return m.group(1)


def main():
    """ Main program body. """

    args = parse_args()
    rebalance(args)


def parse_args():
    """ Parse and return args. """

    parser = argparse.ArgumentParser(description='Re-balance CI group(s) from a downloaded results directory.')

    parser.add_argument('group_count',
                        metavar='group_count',
                        help='The number of groups to re-balance the tests to.')

    parser.add_argument('run', metavar='RUN', type=run_id_arg, help='AZP run id or URI')

    parser.add_argument('-v', '--verbose',
                        dest='verbose',
                        action='store_true',
                        help='Display more detailed info about files being read and edited.')

    parser.add_argument('-t', '--target-path',
                        dest='target_path',
                        required=False,
                        help='The directory where the test targets are located. If set the aliases will automatically '
                             'by rewritten with the new proposed group.')

    if argcomplete:
        argcomplete.autocomplete(parser)

    args = parser.parse_args()

    return args


def datetime_from_ts(ts):
    return datetime.datetime.strptime(ts.split(".")[0].replace("Z", ""), "%Y-%m-%dT%H:%M:%S")


def get_raw_test_targets(args):
    """ Fetch timeline from AZP and preprocess results. """

    target_times = {}

    resp = requests.get('https://dev.azure.com/ansible/ansible/_apis/build/builds/%s/timeline?api-version=6.0' % args.run)
    resp.raise_for_status()
    timeline = resp.json()

    name_matcher = re.compile(r'.* - (\d)')

    for record in timeline['records']:
        name = record['name']
        start = datetime_from_ts(record['startTime'])
        finish = datetime_from_ts(record['finishTime'])
        duration = finish - start

        match = name_matcher.fullmatch(name)
        if match:
            group = match.group(1)
            target_times[group] = max(duration.seconds, target_times.get(group, 0))

    return dict(sorted(target_times.items(), key=lambda i: i[1], reverse=True))


def print_test_runtime(target_times):
    """ Prints a nice summary of a dict containing test target names and their runtime. """
    target_name_max_len = 0
    for target_name in target_times.keys():
        target_name_max_len = max(target_name_max_len, len(target_name))

    print("%s | Seconds |" % ("Target Name".ljust(target_name_max_len),))
    print("%s | ------- |" % ("-" * target_name_max_len,))
    for target_name, target_time in target_times.items():
        print("%s | %s |" % (target_name.ljust(target_name_max_len), str(target_time).ljust(7)))


def rebalance(args):
    """ Prints a nice summary of a proposed rebalanced configuration based on the downloaded CI result. """

    target_times = get_raw_test_targets(args)

    group_info = dict([(i, {'targets': [], 'total_time': 0}) for i in range(1, int(args.group_count) + 1)])

    # Now add each test to the group with the lowest running time.
    for target_name, target_time in target_times.items():
        index, total_time = min(enumerate([g['total_time'] for g in group_info.values()]), key=operator.itemgetter(1))
        group_info[index + 1]['targets'].append(target_name)
        group_info[index + 1]['total_time'] = total_time + target_time

    # Print a summary of the proposed test split.
    for group_number, test_info in group_info.items():
        print("Group %d - Total Runtime (s): %d" % (group_number, test_info['total_time']))
        print_test_runtime(dict([(n, target_times[n]) for n in test_info['targets']]))
        print()

    if args.target_path:
        target_path = os.path.expanduser(os.path.expandvars(args.target_path))

        for test_root in ['test', 'tests']:  # ansible/ansible uses 'test' but collections use 'tests'.
            integration_root = os.path.join(target_path, test_root, 'integration', 'targets')
            if os.path.isdir(integration_root):
                if args.verbose:
                    print("Found test integration target dir at '%s'" % integration_root)
                break

        else:
            # Failed to find test integration target folder
            raise ValueError("Failed to find the test target folder on test/integration/targets or "
                             "tests/integration/targets under '%s'." % target_path)

        for group_number, test_info in group_info.items():
            for test_target in test_info['targets']:
                test_target_aliases = os.path.join(integration_root, test_target, 'aliases')
                if not os.path.isfile(test_target_aliases):
                    if args.verbose:
                        print("Cannot find test target alias file at '%s', skipping." % test_target_aliases)
                    continue

                with open(test_target_aliases, mode='r') as fd:
                    test_aliases = fd.readlines()

                changed = False
                for idx, line in enumerate(test_aliases):
                    group_match = re.match(r'shippable/(.*)/group(\d+)', line)
                    if group_match:
                        if int(group_match.group(2)) != group_number:
                            new_group = 'shippable/%s/group%d\n' % (group_match.group(1), group_number)
                            if args.verbose:
                                print("Changing %s group from '%s' to '%s'" % (test_target, group_match.group(0),
                                                                               new_group.rstrip()))
                            test_aliases[idx] = new_group
                            changed = True
                            break
                else:
                    if args.verbose:
                        print("Test target %s matches proposed group number, no changed required" % test_target)

                if changed:
                    with open(test_target_aliases, mode='w') as fd:
                        fd.writelines(test_aliases)


if __name__ == '__main__':
    main()
