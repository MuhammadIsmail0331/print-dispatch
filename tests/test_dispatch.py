from pathlib import Path
import hashlib
import json
import struct
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from print_dispatch import HandoffError, check_job, inspect_stl, main, render_html, write_pack
from examples.make_example import cuboid


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root/'block.stl'
        cuboid(self.file)
        self.job = {'title':'First fit', 'units':'mm', 'printer_mm':[220,220,250],
                    'parts':[{'file':'block.stl','quantity':2,'material':'PETG','notes':'Keep upright.'}]}
        self.manifest = self.root/'job.json'

    def save(self):
        self.manifest.write_text(json.dumps(self.job), encoding='utf-8')
        return self.manifest

    def test_closed_block_volume_and_quantity(self):
        result = check_job(self.save())
        self.assertEqual(result['status'],'checks_passed')
        self.assertEqual(result['total_pieces'],2)
        part=result['parts'][0]
        self.assertEqual(part['size_mm'],[20,20,10])
        self.assertEqual(part['signed_volume_mm3'],4000)
        self.assertEqual(part['sha256'],hashlib.sha256(self.file.read_bytes()).hexdigest())

    def test_translation_does_not_change_volume_or_size(self):
        cuboid(self.file,offset=(1000,-2000,3000))
        part=inspect_stl(self.file)
        self.assertEqual(part['signed_volume_mm3'],4000)
        self.assertEqual(part['size_mm'],[20,20,10])

    def test_open_mesh_is_reported_and_cannot_be_packed(self):
        cuboid(self.file,omit_last=True)
        self.assertEqual(check_job(self.save())['status'],'needs_attention')
        with self.assertRaises(HandoffError):write_pack(self.save(),self.root/'job.zip')
        self.assertFalse((self.root/'job.zip').exists())

    def test_wrong_winding_is_reported(self):
        data=bytearray(self.file.read_bytes())
        a,b=data[96:108],data[108:120]
        data[96:108],data[108:120]=b,a
        self.file.write_bytes(data)
        self.assertTrue(check_job(self.save())['parts'][0]['inconsistent_edge_count'])

    def test_nan_coordinate_is_rejected(self):
        data=bytearray(self.file.read_bytes());struct.pack_into('<f',data,96,float('nan'));self.file.write_bytes(data)
        with self.assertRaisesRegex(HandoffError,'NaN'):check_job(self.save())

    def test_truncated_stl_is_rejected(self):
        self.file.write_bytes(self.file.read_bytes()[:-1])
        with self.assertRaisesRegex(HandoffError,'file length'):check_job(self.save())

    def test_ascii_stl_is_not_misreported(self):
        self.file.write_text('solid example\nendsolid example\n')
        with self.assertRaises(HandoffError):check_job(self.save())

    def test_oversize_in_supplied_orientation_is_reported(self):
        cuboid(self.file,(230,20,10))
        self.assertIn('exceeds printer',check_job(self.save())['errors'][0])

    def test_unknown_units_are_rejected(self):
        for units in ['inch',None,'']:
            with self.subTest(units=units):
                self.job['units']=units
                with self.assertRaisesRegex(HandoffError,'units'):check_job(self.save())

    def test_invalid_quantities_are_rejected(self):
        for q in [0,-1,True,1.5,'2',10001]:
            with self.subTest(quantity=q):
                self.job['parts'][0]['quantity']=q
                with self.assertRaisesRegex(HandoffError,'Quantity'):check_job(self.save())

    def test_invalid_printer_dimensions_are_rejected(self):
        for bed in [[220,220],[-1,220,250],[True,220,250],[float('inf'),220,250]]:
            with self.subTest(bed=bed):
                self.job['printer_mm']=bed
                with self.assertRaisesRegex(HandoffError,'printer_mm'):check_job(self.save())

    def test_parent_and_absolute_paths_are_rejected(self):
        for name in ['../block.stl','/block.stl','C:/block.stl','sub\\block.stl']:
            with self.subTest(name=name):
                self.job['parts'][0]['file']=name
                with self.assertRaises(HandoffError):check_job(self.save())

    def test_duplicate_files_are_rejected(self):
        self.job['parts'].append(dict(self.job['parts'][0]))
        with self.assertRaisesRegex(HandoffError,'Duplicate'):check_job(self.save())

    def test_material_is_required(self):
        self.job['parts'][0]['material']=''
        with self.assertRaisesRegex(HandoffError,'Material'):check_job(self.save())

    def test_html_escapes_user_content(self):
        self.job['title']='<script>alert(1)</script>'
        output=render_html(check_job(self.save()))
        self.assertNotIn('<script>',output)
        self.assertIn('&lt;script&gt;',output)

    def test_pack_is_portable_complete_and_reproducible(self):
        self.save();one=self.root/'one.zip';two=self.root/'two.zip'
        write_pack(self.manifest,one);write_pack(self.manifest,two)
        self.assertEqual(one.read_bytes(),two.read_bytes())
        extracted=self.root/'extracted'
        with zipfile.ZipFile(one) as z:
            self.assertEqual(set(z.namelist()),{'job.json','report.json','START HERE.html','SHA256SUMS.txt','parts/block.stl'})
            z.extractall(extracted)
        report=check_job(extracted/'job.json')
        self.assertEqual(report['status'],'checks_passed')
        saved=json.loads((extracted/'report.json').read_text())
        self.assertEqual(saved,report)
        for line in (extracted/'SHA256SUMS.txt').read_text().splitlines():
            digest,name=line.split('  ',1)
            self.assertEqual(digest,hashlib.sha256((extracted/name).read_bytes()).hexdigest())

    def test_existing_output_is_never_overwritten(self):
        output=self.root/'existing.zip';output.write_bytes(b'keep')
        with self.assertRaisesRegex(HandoffError,'already exists'):write_pack(self.save(),output)
        self.assertEqual(output.read_bytes(),b'keep')

    def test_cli_rejects_report_zip_collision_before_writing(self):
        output=self.root/'new.zip'
        self.assertEqual(main(['pack',str(self.save()),'-o',str(output),'--report',str(output)]),2)
        self.assertFalse(output.exists())

    def test_cli_does_not_overwrite_the_job_manifest(self):
        self.save();original=self.manifest.read_bytes()
        self.assertEqual(main(['check',str(self.manifest),'--html',str(self.manifest)]),2)
        self.assertEqual(original,self.manifest.read_bytes())


if __name__ == '__main__': unittest.main()
