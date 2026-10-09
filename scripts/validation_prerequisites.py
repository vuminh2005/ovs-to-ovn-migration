#!/usr/bin/env python3
"""Prepare reusable validation image/flavor; independent of workload lifecycle."""
import argparse
import fcntl
import hashlib
import pathlib
import re
import sys
import time
import urllib.request
import uuid
import struct
from types import SimpleNamespace

from workload_validation import save
from workload_resources import image_flavor_compatibility


def qcow_virtual_size(path):
    with path.open('rb') as stream: header=stream.read(32)
    if len(header)!=32 or header[:4]!=b'QFI\xfb':
        raise RuntimeError('Managed image is not a valid QCOW2 header; cannot verify virtual disk size')
    return struct.unpack('!Q',header[24:32])[0]


def exact_image(cloud, selector):
    try:
        uuid.UUID(selector)
    except ValueError:
        matches = [i for i in cloud.image.images(name=selector) if i.name == selector]
        if len(matches) > 1:
            raise RuntimeError(f'Multiple Glance images named {selector!r}; resolve duplicates or configure a UUID')
        return matches[0] if matches else None
    return cloud.image.find_image(selector, ignore_missing=True)


def exact_flavor(cloud, selector):
    matches = [f for f in cloud.compute.flavors(details=True) if f.name == selector or f.id == selector]
    if len(matches) > 1:
        raise RuntimeError(f'Ambiguous flavor {selector!r}; configure a unique flavor UUID')
    return matches[0] if matches else None


def checksum_from_manifest(text, filename):
    matches = []
    for line in text.splitlines():
        match = re.fullmatch(r'([0-9a-fA-F]{64})\s+\*?(.+)', line.strip())
        if match and match[2] == filename:
            matches.append(match[1].lower())
    if len(matches) != 1:
        raise RuntimeError(f'Official SHA256SUMS must contain exactly one entry for {filename}')
    return matches[0]


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            result.update(block)
    return result.hexdigest()


def download_verified(cfg, cache):
    url = cfg['image_url']
    if not url.startswith('https://cloud-images.ubuntu.com/'):
        raise RuntimeError('Managed image URL must use official Ubuntu cloud-images HTTPS')
    filename = url.rsplit('/', 1)[1]
    manifest_url = url.rsplit('/', 1)[0]+'/SHA256SUMS'
    timeout = cfg['download_timeout']
    # Fetch the current official manifest before trusting any local cached file.
    try:
        with urllib.request.urlopen(manifest_url, timeout=timeout) as response:
            manifest = response.read().decode('ascii')
        expected = checksum_from_manifest(manifest, filename)
        target = cache/(expected+'-'+filename)
        if target.exists() and digest(target) == expected:
            return target, expected, manifest_url
        partial = target.with_suffix('.part')
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response, partial.open('wb') as stream:
                for block in iter(lambda: response.read(1024*1024), b''):
                    stream.write(block)
            if digest(partial) != expected:
                raise RuntimeError('Ubuntu image SHA256 mismatch; release may have changed during download; retry')
            partial.chmod(0o600)
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
        return target, expected, manifest_url
    except Exception as exc:
        raise RuntimeError(f'Cannot prepare managed Ubuntu image from {url}: {exc}. Check outbound HTTPS, disk space, and official SHA256SUMS; or configure an existing validation_image') from exc


def active_image(cloud, image, timeout):
    deadline = time.monotonic()+timeout
    while True:
        image = cloud.image.get_image(image.id)
        if image.status.lower() == 'active':
            return image
        if image.status.lower() not in ('queued','saving','uploading','importing'):
            raise RuntimeError(f'Validation image {image.id} is {image.status}; inspect Glance before retrying')
        if time.monotonic() >= deadline:
            raise RuntimeError(f'Image {image.id} did not become ACTIVE in {timeout}s; inspect Glance; no duplicate will be created')
        time.sleep(2)


def prepare(cloud, cfg, root):
    cache = pathlib.Path(cfg['cache_dir']).expanduser().resolve()
    if cache == root.resolve() or root.resolve() in cache.parents:
        raise RuntimeError('validation_image_cache_dir must be outside migration_run_dir')
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Serialize managed-name creation across invocations on this deployment host.
    with (cache/'prerequisites.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        image = exact_image(cloud, cfg['image']) if cfg['image'] else None
        result = {'image': {}, 'flavor': {}, 'cleanup_prerequisites': False}
        virtual_size = None
        if image is None:
            image = exact_image(cloud, cfg['managed_image_name'])
        if image is None:
            path, sha256, manifest = download_verified(cfg, cache)
            if path.exists(): virtual_size = qcow_virtual_size(path)
            # Recheck after download, then SDK duplicate protection before upload.
            image = exact_image(cloud, cfg['managed_image_name'])
            if image is None:
                print('Uploading verified Ubuntu validation image to Glance', flush=True)
                image = cloud.image.create_image(name=cfg['managed_image_name'], filename=str(path),
                    disk_format='qcow2', container_format='bare', allow_duplicates=False,
                    wait=False, architecture='x86_64', os_distro='ubuntu', os_version='24.04')
                result['image'].update(created=True, source_url=cfg['image_url'], sha256=sha256,
                                       checksum_url=manifest, cache_path=str(path))
                result['image'].update(id=image.id, name=image.name)
                save(root/'validation-prerequisites.json', result)
        result['image'].setdefault('created', False)
        image = active_image(cloud, image, cfg['image_active_timeout'])
        result['image'].update(id=image.id, name=image.name, status=image.status)
        save(root/'validation-prerequisites.json', result)
        flavor = exact_flavor(cloud, cfg['flavor']) if cfg['flavor'] else None
        if cfg['flavor'] and flavor is None and cfg['flavor'] != cfg['managed_flavor_name']:
            raise RuntimeError(f'Configured validation flavor {cfg["flavor"]!r} was not found; correct override or leave it empty for automatic preparation')
        managed = not cfg['flavor'] or cfg['flavor'] == cfg['managed_flavor_name'] or (flavor is not None and flavor.name == cfg['managed_flavor_name'])
        if flavor is None:
            flavor = exact_flavor(cloud, cfg['managed_flavor_name'])
        if flavor is not None and managed:
            if (int(flavor.vcpus), int(flavor.ram), int(flavor.disk)) != (1,1024,8):
                raise RuntimeError(f'Managed flavor {flavor.name!r} must have 1 vCPU, 1024 MB RAM and 8 GB disk; existing properties are incompatible')
        if flavor is None:
            image_flavor_compatibility(image, SimpleNamespace(disk=8,ram=1024), virtual_size)
            flavor = cloud.compute.create_flavor(name=cfg['managed_flavor_name'], vcpus=1, ram=1024, disk=8)
            result['flavor']['created'] = True
        result['flavor'].setdefault('created', False)
        result['flavor'].update(id=flavor.id, name=flavor.name, vcpus=int(flavor.vcpus),
                                ram_mb=int(flavor.ram), disk_gb=int(flavor.disk))
        result['image_flavor_compatibility'] = image_flavor_compatibility(image, flavor, virtual_size)
        save(root/'validation-prerequisites.json', result)
        return result


def main():
    import json
    import openstack
    p = argparse.ArgumentParser()
    p.add_argument('root', type=pathlib.Path)
    args = p.parse_args()
    cfg = json.loads((args.root/'validation-prerequisites-config.json').read_text())
    try:
        prepare(openstack.connect(), cfg, args.root)
    except Exception as exc:
        print(f'Validation prerequisite preparation failed BEFORE migration: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
