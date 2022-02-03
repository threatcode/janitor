#!/usr/bin/python3

import argparse
import os
import pwd
import shutil
import subprocess

from iniparse import RawConfigParser
from janitor.config import read_config


def create_chroot(distro, sbuild_path, suites, sbuild_arch, include=[], eatmydata=True, make_sbuild_tarball=None):
    cmd = ["sbuild-createchroot", distro.name, sbuild_path, distro.archive_mirror_uri]
    cmd.append("--components=%s" % ','.join(distro.component))
    if eatmydata:
        cmd.append("--command-prefix=eatmydata")
        include = list(include) + ["eatmydata"]
    if include:
        cmd.append("--include=%s" % ','.join(include))
    for suite in suites:
        cmd.append("--alias=%s-%s-sbuild" % (suite, sbuild_arch))
    if make_sbuild_tarball:
        cmd.append("--make-sbuild-tarball=%s" % make_sbuild_tarball)
    subprocess.check_call(cmd)


def get_sbuild_architecture():
    return subprocess.check_output(["dpkg-architecture", "-qDEB_BUILD_ARCH"]).decode().strip()


parser = argparse.ArgumentParser()
parser.add_argument('--remove-old', action='store_true')
parser.add_argument('--include', type=str, action='append', help='Include package.', default=[])
parser.add_argument('--base-directory', type=str, help='Base directory for chroots')
parser.add_argument('--user', type=str, help='User to create home directory for')
parser.add_argument('--make-sbuild-tarball', type=str, help='Create sbuild tarball')
parser.add_argument(
    "--config", type=str, default="janitor.conf", help="Path to configuration."
)
parser.add_argument("sbuild_path", type=str, nargs="?")
args = parser.parse_args()

with open(args.config, "r") as f:
    config = read_config(f)

sbuild_arch = get_sbuild_architecture()
if args.sbuild_path is None:
    if not args.base_directory:
        parser.print_usage()
        parser.exit()
    sbuild_path = os.path.join(args.base_directory, config.distribution.chroot)
else:
    sbuild_path = args.sbuild_path

if args.remove_old:
    for entry in os.scandir('/etc/schroot/chroot.d'):
        cp = RawConfigParser()
        cp.read([entry.path])
        if config.distribution.chroot in cp.sections():
            old_sbuild_path = cp.get('unstable-amd64-sbuild', 'directory')
            if old_sbuild_path != sbuild_path:
                raise AssertionError('sbuild path has changed: %s != %s' % (
                    old_sbuild_path, sbuild_path))
            if os.path.isdir(old_sbuild_path):
                shutil.rmtree(old_sbuild_path)
            os.unlink(entry.path)

suites = []
for suite in config.suite:
    if not suite.debian_build:
        continue
    suites.append(suite.debian_build.build_distribution)
for campaign in config.campaign:
    if not campaign.debian_build:
        continue
    suites.append(campaign.debian_build.build_distribution)
create_chroot(config.distribution, sbuild_path, suites, sbuild_arch, args.include, make_sbuild_tarball=args.make_sbuild_tarball)

if args.user:
    subprocess.check_call(
        ['schroot', '-c', '%s-%s-sbuild' % (config.suite.build_distribution, sbuild_arch),
         '--directory', '/', '--', 'install', '-d', '--owner=%s' % args.user,
         pwd.getpwnam(args.user).pw_dir])
