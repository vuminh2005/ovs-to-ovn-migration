import hashlib
import importlib.util
import io
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
spec=importlib.util.spec_from_file_location('prerequisites',ROOT/'scripts/validation_prerequisites.py')
p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p)


class PrerequisiteTests(unittest.TestCase):
    def setup_cloud(self, root):
        cfg=dict(image='',flavor='',managed_image_name='ovn-validation-ubuntu-24.04',
                 managed_flavor_name='ovn-validation.small',cache_dir=str(root/'cache'),
                 image_url='https://cloud-images.ubuntu.com/releases/noble/release/ubuntu-24.04-server-cloudimg-amd64.img',
                 download_timeout=1,image_active_timeout=1)
        image=SimpleNamespace(id='image-uuid',name=cfg['managed_image_name'],status='active')
        flavor=SimpleNamespace(id='flavor-uuid',name=cfg['managed_flavor_name'],vcpus=1,ram=1024,disk=8)
        cloud=SimpleNamespace(image=Mock(),compute=Mock())
        cloud.image.images.return_value=[image]; cloud.image.get_image.return_value=image
        cloud.compute.flavors.return_value=[flavor]
        return cfg,cloud,image,flavor

    def test_reuse_managed_without_internet_or_creation(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d); run=root/'run'; run.mkdir()
            cfg,cloud,_,_=self.setup_cloud(root)
            with patch.object(p,'download_verified') as download:
                result=p.prepare(cloud,cfg,run); p.prepare(cloud,cfg,run)
                download.assert_not_called()
            cloud.image.create_image.assert_not_called(); cloud.compute.create_flavor.assert_not_called()
            self.assertEqual(result['image']['id'],'image-uuid')
            self.assertEqual(result['flavor']['id'],'flavor-uuid')
            self.assertTrue((run/'validation-prerequisites.json').exists())

    def test_default_creation_and_recheck(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d); run=root/'run'; run.mkdir()
            cfg,cloud,image,flavor=self.setup_cloud(root)
            cloud.image.images.return_value=[]; cloud.compute.flavors.return_value=[]
            cloud.image.create_image.return_value=image; cloud.compute.create_flavor.return_value=flavor
            with patch.object(p,'download_verified',return_value=(root/'image.img','sha','manifest')):
                result=p.prepare(cloud,cfg,run)
            self.assertTrue(result['image']['created']); self.assertTrue(result['flavor']['created'])
            self.assertEqual(cloud.image.images.call_count,2)
            self.assertFalse(cloud.image.create_image.call_args.kwargs['allow_duplicates'])
            self.assertEqual(cloud.compute.create_flavor.call_args.kwargs['disk'],8)

    def test_external_exact_name_reuse(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d); run=root/'run'; run.mkdir()
            cfg,cloud,image,flavor=self.setup_cloud(root)
            cfg.update(image='external-image',flavor='external-flavor')
            image.name=cfg['image']; flavor.name=cfg['flavor']; flavor.ram=2048
            result=p.prepare(cloud,cfg,run)
            self.assertEqual(result['image']['name'],'external-image')
            cloud.image.create_image.assert_not_called()

    def test_duplicate_managed_image_fails(self):
        with tempfile.TemporaryDirectory() as d:
            cfg,cloud,image,_=self.setup_cloud(pathlib.Path(d))
            cloud.image.images.return_value=[image,image]
            with self.assertRaisesRegex(RuntimeError,'Multiple Glance'):
                p.exact_image(cloud,cfg['managed_image_name'])
            cloud.image.create_image.assert_not_called()

    def test_incompatible_managed_flavor_fails(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d); run=root/'run'; run.mkdir()
            cfg,cloud,_,flavor=self.setup_cloud(root); flavor.disk=4
            with self.assertRaisesRegex(RuntimeError,'incompatible'): p.prepare(cloud,cfg,run)
            cloud.compute.create_flavor.assert_not_called()

    def test_checksum_parser(self):
        expected='a'*64
        self.assertEqual(p.checksum_from_manifest(expected+' *image.img\n','image.img'),expected)
        with self.assertRaises(RuntimeError): p.checksum_from_manifest(expected+' *other.img','image.img')

    def test_verified_download_and_cache(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d); cfg,_,_,_=self.setup_cloud(root); cfg['cache_dir']=d
            payload=b'qcow2-fixture'; sha=hashlib.sha256(payload).hexdigest()
            manifest=(sha+' *ubuntu-24.04-server-cloudimg-amd64.img\n').encode()
            with patch.object(p.urllib.request,'urlopen',side_effect=[io.BytesIO(manifest),io.BytesIO(payload),io.BytesIO(manifest)]) as request:
                target,actual,_=p.download_verified(cfg,root)
                p.download_verified(cfg,root)
                self.assertEqual(request.call_count,3)  # cached image skips image GET
                self.assertEqual(target.read_bytes(),payload); self.assertEqual(actual,sha)

    def test_checksum_failure_never_returns_image(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d); cfg,_,_,_=self.setup_cloud(root)
            manifest=('a'*64+' *ubuntu-24.04-server-cloudimg-amd64.img\n').encode()
            with patch.object(p.urllib.request,'urlopen',side_effect=[io.BytesIO(manifest),io.BytesIO(b'bad')]):
                with self.assertRaisesRegex(RuntimeError,'SHA256 mismatch'): p.download_verified(cfg,root)
            self.assertFalse(list(root.glob('*.part')))

    def test_sequential_imports_and_helper_names(self):
        import yaml
        imports=[pathlib.Path(i['import_playbook']) for i in yaml.safe_load((ROOT/'migrate-to-ovn.yml').read_text())]
        self.assertEqual([int(i.name[:2]) for i in imports],list(range(14)))
        self.assertTrue(all((ROOT/i).exists() for i in imports))
        self.assertTrue((ROOT/'playbooks/resume-bootstrap.yml').exists())
        self.assertTrue((ROOT/'playbooks/validation-snapshot-tasks.yml').exists())
        cutover=yaml.safe_load((ROOT/'playbooks/08-cutover.yml').read_text())
        self.assertEqual(cutover[0]['tasks'][0]['ansible.builtin.command']['argv'][4:6], ['08','start'])


if __name__=='__main__': unittest.main()
