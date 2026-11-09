#!/usr/bin/env python3
"""Publish pinned RT-Embed or RT-CV assets from the dev container; no archives.

Requires host boto3, Docker access, and the course bucket credentials. Does not
read container credentials or recursively upload model/cache directories.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service', choices=('rt-embed', 'rt-cv'), default='rt-embed')
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--lab-container', default='x-ov-63-v1-task1_lab_1')
    parser.add_argument('--embed-container', help='Source container override (also accepted for RT-CV)')
    args = parser.parse_args()
    args.manifest = args.manifest or Path(__file__).resolve().parents[1] / ('deploy/' + args.service + '-checkpoint.json')
    args.embed_container = args.embed_container or ('vss-rtvi-cv' if args.service == 'rt-cv' else 'vss-rtvi-embed')
    manifest = json.loads(args.manifest.read_text())
    # Every source path comes from a repository-owned manifest, not a bucket listing.
    sources = {}
    for item in manifest['files']:
        path = PurePosixPath(item['name'])
        if '..' in path.parts or path.is_absolute():
            raise ValueError('Unsafe manifest path')
        if args.service == 'rt-cv':
            sources[item['name']] = '/opt/storage/' + str(path)
            continue
        if path.parts[:2] == ('checkpoint', manifest['model_directory']):
            root = '/opt/nvidia/rtvi/.rtvi/ngc_model_cache'
        elif path.parts[:2] == ('triton', manifest['repository_directory']):
            root = '/tmp/triton_model_repo'
        else:
            raise ValueError('Unrecognized artifact path')
        sources[item['name']] = root + '/' + str(PurePosixPath(*path.parts[1:]))
    s3 = boto3.client('s3')
    prefix = 'data/x-ov-63-v1-models/' + args.service + '/' + manifest['version'] + '/' + manifest['directory'] + '/'

    def publish(item):
        key = prefix + item['name']
        try:
            existing = s3.head_object(Bucket='dli-lms', Key=key)
        except ClientError as exc:
            if exc.response['Error']['Code'] not in ('404', 'NoSuchKey'):
                raise
        else:
            if existing['ContentLength'] != item['bytes'] or existing.get('Metadata', {}).get('sha256') != item['sha256']:
                raise RuntimeError('Refusing to overwrite a different existing artifact: ' + key)
            print('Already published:', item['name'], flush=True)
            return
        command = ['docker', 'exec', args.lab_container, 'docker', 'exec',
                   args.embed_container, 'cat', '--', sources[item['name']]]
        digest = hashlib.sha256()
        length = 0
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
            class CheckedStream:
                def read(self, size=-1):
                    nonlocal length
                    data = proc.stdout.read(size)
                    digest.update(data)
                    length += len(data)
                    return data
            s3.upload_fileobj(CheckedStream(), 'dli-lms', key,
                             ExtraArgs={'ServerSideEncryption': 'AES256',
                                        'Metadata': {'sha256': item['sha256']}},
                             Config=TransferConfig(use_threads=False))
            error = proc.stderr.read().decode()
            if proc.wait() != 0:
                raise RuntimeError('Artifact read failed: ' + error)
        if length != item['bytes'] or digest.hexdigest() != item['sha256']:
            raise RuntimeError('Source changed during upload; published artifact must not be used: ' + key)
        head = s3.head_object(Bucket='dli-lms', Key=key)
        assert head['ContentLength'] == item['bytes'] and head['ServerSideEncryption'] == 'AES256'
        print('Published:', item['name'], item['bytes'], 'bytes', flush=True)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(publish, manifest['files']))
    print('Published pinned cache:', len(manifest['files']), 'files,',
          sum(f['bytes'] for f in manifest['files']), 'bytes')


if __name__ == '__main__':
    main()
