#!/usr/bin/env python3
#
# Copyright(c) 2022 Association of Universities for Research in Astronomy, Inc.

"""
A script for maintaining a simple GemVM configuration file, so that users can
conveniently specify one or more disk images and the associated configuration
via a short name label. The motivation is partly to avoid routine manipulation
of the paths to disk images that must be protected from inadvertent deletion.

Much of the logic lives in gemvm.py, allowing the latter also to function as a
stand-alone script outside the package (eg. using the OS python), if convenient.
"""

import argparse
import json
import os
import sys

from .gemvm import config_file, get_config, _add_main_args, _merge_args

indent = 4


def confirm(prompt):

    while True:

        try:
            answer = input(f'{prompt} (y/[n]): ').lower()
        except (EOFError, KeyboardInterrupt):
            answer = ''
            print()

        if answer in ('y', 'yes'):
            return True
        elif answer in ('n', 'no', ''):
            return False


def write_config(config, filename):

    with open(filename, mode='w') as config_fd:
        config_fd.write(json.dumps(config, indent=indent))


def main():

    script_name = os.path.basename(sys.argv[0])

    parser = argparse.ArgumentParser(
        description='A script for maintaining the gemvm configuration file'
    )
    subparsers = parser.add_subparsers(dest='cmd', required=True,
                                       help='operation to perform')

    name_args = {'dest' : 'name', 'type' : str,
                 'help' : 'name assigned to the VM definition / disk image(s)'}

    parser_add = subparsers.add_parser('add',
                                       help='add/update a VM configuration')
    parser_add.add_argument(**name_args)
    _add_main_args(parser_add, lookup=False)

    parser_del = subparsers.add_parser('del',
                                       help='delete VM configuration(s)')
    parser_del.add_argument(**name_args, nargs='?')

    parser_list = subparsers.add_parser('list',
                                        help='list VM configuration(s)')
    parser_list.add_argument(**name_args, nargs='?')

    args = parser.parse_args()

    # Read any existing config, defaulting to an empty one:
    config, conf_errs = get_config(config_file)

    # Produce error if user tries to list or delete a non-existent entry:
    if args.name:
        if args.cmd in ('del', 'list') and args.name not in config['names']:
            sys.stderr.write(f"{script_name}: entry '{args.name}' not found\n")
            sys.exit(1)

    # Add/update an entry:
    if args.cmd == 'add':

        if conf_errs:
            sys.stderr.write(f"{script_name}: can't update corrupt config; "
                             f"delete it (or fix manually) first\n")
            sys.exit(1)

        vm_args = _merge_args(args)

        modified = True
        if args.name in config['names']:
            if not confirm(f'Replace existing entry {args.name}?'):
                modified = False
                print('Aborted')

        if modified:
            config['names'][args.name] = vm_args

    # Delete one or all entries:
    elif args.cmd == 'del':

        if args.name:
            modified = confirm(f'Delete entry {args.name}?')
        else:
            modified = confirm('Delete ALL config entries?')

        if modified:
            if args.name is None:
                config['names'] = {}
            else:
                del config['names'][args.name]
        else:
            print('Aborted')

    # List existing entries (creating an empty config if there is none):
    elif args.cmd == 'list':

        modified = False

        if args.name is None:
            section = config['names']
        else:
            section = {
                args.name : config['names'][args.name]
            }

        # TO DO: tidy up print format
        for name, vals in section.items():
            try:
                assert isinstance(vals, dict)
            except AssertionError:
                sys.stderr.write(
                    f"{script_name}: invalid entry for '{name}'\n\n"
                )
            else:
                print(name)
                for kw in vals:
                    print(f'    {kw}={vals[kw]}')
                print()

    # Save updated config, if applicable:
    if modified:
        write_config(config, config_file)


if __name__ == '__main__':
    main()
