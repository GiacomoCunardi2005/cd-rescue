import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cdrescue as cr  # noqa: E402

S = cr.SECTOR


class FakeDisc:
    """Scratched disc: `bad` sectors never read, `flaky` sectors fail N times then work."""

    def __init__(self, data, bad=(), flaky=None):
        self.data, self.size = data, len(data)
        self.bad, self.flaky = set(bad), dict(flaky or {})
        self.speeds = []

    def read(self, off, size):
        secs = range(off // S, -(-(off + size) // S))
        if any(s in self.bad for s in secs):
            raise OSError(5, "EIO")
        for s in secs:
            if self.flaky.get(s, 0) > 0:
                self.flaky[s] -= 1
                raise OSError(5, "EIO")
        return self.data[off:off + size]

    def drop_cache(self):
        pass

    def set_speed(self, x):
        self.speeds.append(x)


class RescueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.img, self.map = os.path.join(self.tmp, "d.iso"), os.path.join(self.tmp, "d.map")
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_recovers_everything_but_dead_sectors(self):
        data = os.urandom(300 * S + 100)  # partial last sector
        disc = FakeDisc(data, bad={40, 41, 200}, flaky={10: 2, 150: 1})
        st = cr.rescue(disc, self.img, self.map, retries=2)
        got = open(self.img, "rb").read()
        self.assertEqual(len(got), len(data))
        for i in range(len(st)):
            want = b"-" if i in (40, 41, 200) else b"+"
            self.assertEqual(st[i:i + 1], want, i)
            if want == b"+":
                self.assertEqual(got[i * S:(i + 1) * S], data[i * S:(i + 1) * S])
            else:
                self.assertEqual(got[i * S:(i + 1) * S], bytes(S))
        self.assertEqual(disc.speeds, [1, 0])
        self.assertEqual(cr.bad_ranges(st), [(40, 42), (200, 201)])

    def test_map_roundtrip_and_resume(self):
        data = os.urandom(100 * S)
        cr.rescue(FakeDisc(data, bad={5, 6, 7}), self.img, self.map, retries=0)
        text = open(self.map).read()
        self.assertIn("0x00002800  0x00001800  -", text)  # ddrescue format: sectors 5..7
        # second run with the disc now readable only fills the holes
        disc = FakeDisc(data)
        reads = []
        orig = disc.read
        disc.read = lambda o, s: reads.append(o) or orig(o, s)
        st = cr.rescue(disc, self.img, self.map)
        self.assertEqual(st.count(b"+"), 100)
        self.assertEqual(open(self.img, "rb").read(), data)
        self.assertTrue(all(o // S < 32 for o in reads))  # only the cluster holding the holes

    def test_quick_skips_trim(self):
        data = os.urandom(64 * S)
        st = cr.rescue(FakeDisc(data, bad={3}), self.img, self.map, quick=True)
        self.assertEqual(st[:32], b"*" * 32)  # whole cluster lost, never split
        self.assertEqual(st[32:], b"+" * 32)

    def test_load_map_maps_ddrescue_statuses(self):
        with open(self.map, "w") as f:
            f.write("# x\n0x00000000 ? 1\n0x00000000 0x00001000 +\n0x00001000 0x00000800 /\n"
                    "0x00001800 0x00000800 F\n")
        self.assertEqual(bytes(cr.load_map(self.map, 4)), b"++*?")


class CarveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_carves_aligned_files_only(self):
        png = b"\x89PNG\r\n\x1a\n" + os.urandom(3000) + b"IEND\xaeB`\x82"
        jpg = b"\xff\xd8\xff\xe0" + b"\xff\xd8\xff" + b"thumb\xff\xd9" + os.urandom(500) + b"\xff\xd9"
        img = bytearray(10 * S)
        img[2 * S:2 * S + len(png)] = png
        img[5 * S:5 * S + len(jpg)] = jpg
        img[8 * S + 7:8 * S + 15] = b"\x89PNG\r\n\x1a\n"  # unaligned: ignored
        p = os.path.join(self.tmp, "i.iso")
        open(p, "wb").write(img)
        found = sorted(cr.carve(p, os.path.join(self.tmp, "c")))
        self.assertEqual([os.path.basename(f) for f in found], ["00000002.png", "00000005.jpg"])
        self.assertEqual(open(found[0], "rb").read(), png)
        self.assertEqual(open(found[1], "rb").read(), jpg)  # thumbnail footer not used

    def test_vob_sectors_survive_holes(self):
        def pack(n):
            return cr.MPEG_PACK + bytes([n]) * (S - 4)
        secs = [bytes(S)] * 3 + [pack(i % 256) for i in range(600)]
        secs[100] = bytes(S)  # unreadable sector inside the video: dropped
        secs += [bytes(S)] * 300 + [pack(7)] * 600  # far away: second stream
        p = os.path.join(self.tmp, "v.iso")
        open(p, "wb").write(b"".join(secs))
        streams = cr.carve_mpeg(p, os.path.join(self.tmp, "v"), min_size=S)
        self.assertEqual([os.path.basename(s) for s in streams], ["lba_00000003.vob", "lba_00000903.vob"])
        self.assertEqual(os.path.getsize(streams[0]), 599 * S)


@unittest.skipUnless(shutil.which("systemd-inhibit"), "needs systemd")
class KeepAwakeTest(unittest.TestCase):
    def listed(self):
        return "cdrescue" in subprocess.run(["systemd-inhibit", "--list"],
                                            capture_output=True, text=True).stdout

    def test_lock_held_then_released(self):
        p = cr.keep_awake()
        if p is None:
            self.skipTest("logind refused the lock here")
        self.assertTrue(self.listed())
        p.terminate()
        p.wait()
        self.assertFalse(self.listed())


@unittest.skipUnless(shutil.which("xorriso"), "needs xorriso")
class EndToEndTest(unittest.TestCase):
    def test_image_pipeline(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        root = os.path.join(tmp, "root", "VIDEO_TS")
        os.makedirs(root)
        vob = b"".join(cr.MPEG_PACK + os.urandom(S - 4) for _ in range(600))
        open(os.path.join(root, "VTS_01_1.VOB"), "wb").write(vob)
        open(os.path.join(tmp, "root", "note.txt"), "w").write("ciao")
        iso = os.path.join(tmp, "disc.iso")
        subprocess.run(["xorriso", "-as", "mkisofs", "-quiet", "-o", iso, os.path.join(tmp, "root")],
                       check=True, capture_output=True)
        out = os.path.join(tmp, "out")
        self.assertEqual(cr.main([iso, "-o", out]), 0)
        self.assertTrue(os.path.exists(os.path.join(out, "report.json")))
        self.assertEqual(len(os.listdir(os.path.join(out, "vob_sectors"))), 1)
        ext = cr.file_extents(iso)
        lba = {p: (a, b) for p, a, b in ext}["/VIDEO_TS/VTS_01_1.VOB"]
        st = bytearray(b"+" * (os.path.getsize(iso) // S))
        st[lba[0] + 10] = ord("-")
        self.assertEqual(cr.damaged_files(ext, st), [("/VIDEO_TS/VTS_01_1.VOB", 1, 600)])
        if shutil.which("7z"):
            got = open(os.path.join(out, "files", "7z", "VIDEO_TS", "VTS_01_1.VOB"), "rb").read()
            self.assertEqual(got, vob)


if __name__ == "__main__":
    unittest.main()
