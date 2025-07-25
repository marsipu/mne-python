# Authors: The MNE-Python contributors.
# License: BSD-3-Clause
# Copyright the MNE-Python contributors.

import shutil
from contextlib import nullcontext
from functools import partial
from itertools import chain
from pathlib import Path
from string import ascii_lowercase

import matplotlib.pyplot as plt
import numpy as np
import pytest
from numpy.testing import (
    assert_allclose,
    assert_array_equal,
    assert_array_less,
    assert_equal,
)

import mne.channels.montage
from mne import (
    __file__ as _mne_file,
)
from mne import (
    create_info,
    pick_types,
    read_evokeds,
)
from mne._fiff._digitization import (
    _count_points_by_type,
    _format_dig_points,
    _get_dig_eeg,
    _get_fid_coords,
)
from mne._fiff.constants import FIFF
from mne.bem import _fit_sphere
from mne.channels import (
    DigMontage,
    compute_dev_head_t,
    compute_native_head_t,
    get_builtin_montages,
    make_dig_montage,
    make_standard_montage,
    read_custom_montage,
    read_dig_captrak,
    read_dig_dat,
    read_dig_egi,
    read_dig_fif,
    read_dig_hpts,
    read_dig_localite,
    read_dig_polhemus_isotrak,
    read_polhemus_fastscan,
)
from mne.channels.montage import (
    _BUILTIN_STANDARD_MONTAGES,
    _check_get_coord_frame,
    transform_to_head,
    write_dig,
)
from mne.coreg import get_mni_fiducials
from mne.datasets import testing
from mne.io import (
    RawArray,
    read_fiducials,
    read_raw_brainvision,
    read_raw_egi,
    read_raw_fif,
    read_raw_nirx,
)
from mne.io.kit import read_mrk
from mne.preprocessing import compute_current_source_density
from mne.transforms import _ensure_trans, _get_trans, apply_trans, invert_transform
from mne.utils import _record_warnings, assert_dig_allclose
from mne.utils._testing import assert_object_equal
from mne.viz._3d import _fiducial_coords

data_path = testing.data_path(download=False)
fif_dig_montage_fname = data_path / "montage" / "eeganes07.fif"
egi_dig_montage_fname = data_path / "montage" / "coordinates.xml"
egi_raw_fname = data_path / "montage" / "egi_dig_test.raw"
egi_fif_fname = data_path / "montage" / "egi_dig_raw.fif"
bvct_dig_montage_fname = data_path / "montage" / "captrak_coords.bvct"
bv_raw_fname = data_path / "montage" / "bv_dig_test.vhdr"
bv_fif_fname = data_path / "montage" / "bv_dig_raw.fif"
locs_montage_fname = data_path / "EEGLAB" / "test_chans.locs"
evoked_fname = data_path / "montage" / "level2_raw-ave.fif"
eeglab_fname = data_path / "EEGLAB" / "test_raw.set"
fnirs_dname = data_path / "NIRx" / "nirscout" / "nirx_15_2_recording_w_short"
mgh70_fname = data_path / "SSS" / "mgh70_raw.fif"
subjects_dir = data_path / "subjects"

io_dir = Path(__file__).parents[2] / "io"
kit_dir = io_dir / "kit" / "tests" / "data"
elp = kit_dir / "test_elp.txt"
hsp = kit_dir / "test_hsp.txt"
hpi = kit_dir / "test_mrk.sqd"
bv_fname = io_dir / "brainvision" / "tests" / "data" / "test.vhdr"
fif_fname = io_dir / "tests" / "data" / "test_raw.fif"
edf_path = io_dir / "edf" / "tests" / "data" / "test.edf"
bdf_path = io_dir / "edf" / "tests" / "data" / "test_bdf_eeglab.mat"
vhdr_path = io_dir / "brainvision" / "tests" / "data" / "test.vhdr"
ctf_fif_fname = io_dir / "tests" / "data" / "test_ctf_comp_raw.fif"


def _make_toy_raw(n_channels):
    return RawArray(
        data=np.empty([n_channels, 1]),
        info=create_info(
            ch_names=list(ascii_lowercase[:n_channels]), sfreq=1, ch_types="eeg"
        ),
    )


def _make_toy_dig_montage(n_channels, **kwargs):
    return make_dig_montage(
        ch_pos=dict(
            zip(
                list(ascii_lowercase[:n_channels]),
                np.arange(n_channels * 3).reshape(n_channels, 3),
            )
        ),
        **kwargs,
    )


def _get_dig_montage_pos(montage):
    return np.array([d["r"] for d in _get_dig_eeg(montage.dig)])


def test_dig_montage_trans(tmp_path):
    """Test getting a trans from and applying a trans to a montage."""
    nasion, lpa, rpa, *ch_pos = np.random.RandomState(0).randn(10, 3)
    ch_pos = {f"EEG{ii:3d}": pos for ii, pos in enumerate(ch_pos, 1)}
    montage = make_dig_montage(
        ch_pos, nasion=nasion, lpa=lpa, rpa=rpa, coord_frame="mri"
    )
    trans = compute_native_head_t(montage)
    _ensure_trans(trans)
    # ensure that we can save and load it, too
    fname = tmp_path / "temp-mon.fif"
    with pytest.warns(RuntimeWarning, match="MNE naming conventions"):
        _check_roundtrip(montage, fname, "mri")
    # test applying a trans
    position1 = montage.get_positions()
    montage.apply_trans(trans)
    assert montage.get_positions()["coord_frame"] == "head"
    montage.apply_trans(invert_transform(trans))
    position2 = montage.get_positions()
    assert str(position1) == str(position2)  # exactly equal


@pytest.mark.parametrize("fname", (fif_fname, ctf_fif_fname))
def test_fiducials(tmp_path, fname):
    """Test handling of fiducials."""
    # Eventually the code used here should be unified with montage.py, but for
    # now it uses code in odd places
    fids, coord_frame = read_fiducials(fname)
    assert coord_frame == FIFF.FIFFV_COORD_HEAD
    points = _fiducial_coords(fids, coord_frame)
    assert points.shape == (3, 3)
    # Fids
    assert_allclose(points[:, 2], 0.0, atol=1e-6)
    assert_allclose(points[::2, 1], 0.0, atol=1e-6)
    assert points[2, 0] > 0  # RPA
    assert points[0, 0] < 0  # LPA
    # Nasion
    assert_allclose(points[1, 0], 0.0, atol=1e-6)
    assert points[1, 1] > 0
    fname_out = tmp_path / "test-dig.fif"
    make_dig_montage(
        lpa=fids[0]["r"], nasion=fids[1]["r"], rpa=fids[2]["r"], coord_frame="mri_voxel"
    ).save(fname_out, overwrite=True)
    fids_2, coord_frame_2 = read_fiducials(fname_out)
    assert coord_frame_2 == FIFF.FIFFV_MNE_COORD_MRI_VOXEL
    assert_allclose(
        [fid["r"] for fid in fids[:3]],
        [fid["r"] for fid in fids_2],
        rtol=1e-6,
    )
    assert coord_frame_2 is not None


def test_documented():
    """Test that standard montages are documented."""
    montage_dir = Path(_mne_file).parent / "channels" / "data" / "montages"
    montage_files = Path(montage_dir).glob("*")
    montage_names = [f.stem for f in montage_files]

    assert len(montage_names) == len(_BUILTIN_STANDARD_MONTAGES)
    assert set(montage_names) == set([m.name for m in _BUILTIN_STANDARD_MONTAGES])


@pytest.mark.parametrize(
    "reader, file_content, expected_dig, ext, warning",
    [
        pytest.param(
            partial(read_custom_montage, head_size=None),
            (
                "FidNz 0       9.071585155     -2.359754454\n"
                "FidT9 -6.711765       0.040402876     -3.251600355\n"
                "very_very_very_long_name -5.831241498 -4.494821698  4.955347697\n"
                "Cz 0       0       1\n"
                "Cz 0       0       8.899186843"
            ),
            make_dig_montage(
                ch_pos={
                    "very_very_very_long_name": [
                        -5.8312416,
                        -4.4948215,
                        4.9553475,
                    ],  # noqa
                    "Cz": [0.0, 0.0, 8.899187],
                },
                nasion=[0.0, 9.071585, -2.3597546],
                lpa=[-6.711765, 0.04040287, -3.2516003],
                rpa=None,
            ),
            "sfp",
            (RuntimeWarning, r"Duplicate.*last will be used for Cz \(2\)"),
            id="sfp_duplicate",
        ),
        pytest.param(
            partial(read_custom_montage, head_size=None),
            (
                "FidNz 0       9.071585155     -2.359754454\n"
                "FidT9 -6.711765       0.040402876     -3.251600355\n"
                "headshape 1 2 3\n"
                "headshape 4 5 6\n"
                "Cz 0       0       8.899186843"
            ),
            make_dig_montage(
                hsp=[
                    [1, 2, 3],
                    [4, 5, 6],
                ],
                ch_pos={
                    "Cz": [0.0, 0.0, 8.899187],
                },
                nasion=[0.0, 9.071585, -2.3597546],
                lpa=[-6.711765, 0.04040287, -3.2516003],
                rpa=None,
            ),
            "sfp",
            None,
            id="sfp_headshape",
        ),
        pytest.param(
            partial(read_custom_montage, head_size=1),
            (
                "1	       0	 0.50669	     FPz\n"
                "2	      23	 0.71	    	EOG1\n"
                "3	 -39.947	 0.34459	      F3\n"
                "4	       0	 0.25338	      Fz\n"
            ),
            make_dig_montage(
                ch_pos={
                    "EOG1": [0.30873816, 0.72734152, -0.61290705],
                    "F3": [-0.56705965, 0.67706631, 0.46906776],
                    "FPz": [0.0, 0.99977915, -0.02101571],
                    "Fz": [0.0, 0.71457525, 0.69955859],
                },
                nasion=None,
                lpa=None,
                rpa=None,
                coord_frame="head",
            ),
            "loc",
            None,
            id="EEGLAB",
        ),
        pytest.param(
            partial(read_custom_montage, head_size=None, coord_frame="mri"),
            "// MatLab   Sphere coordinates [degrees]         Cartesian coordinates\n"  # noqa: E501
            "// Label       Theta       Phi    Radius         X         Y         Z       off sphere surface\n"  # noqa: E501
            "E1      37.700     -14.000       1.000    0.7677    0.5934   -0.2419  -0.00000000000000011\n"  # noqa: E501
            "E3      51.700      11.000       1.000    0.6084    0.7704    0.1908   0.00000000000000000\n"  # noqa: E501
            "E31      90.000     -11.000       1.000    0.0000    0.9816   -0.1908   0.00000000000000000\n"  # noqa: E501
            "E61     158.000     -17.200       1.000   -0.8857    0.3579   -0.2957  -0.00000000000000022",  # noqa: E501
            make_dig_montage(
                ch_pos={
                    "E1": [0.7677, 0.5934, -0.2419],
                    "E3": [0.6084, 0.7704, 0.1908],
                    "E31": [0.0, 0.9816, -0.1908],
                    "E61": [-0.8857, 0.3579, -0.2957],
                },
                nasion=None,
                lpa=None,
                rpa=None,
                coord_frame="mri",
            ),
            "csd",
            None,
            id="matlab",
        ),
        pytest.param(
            partial(read_custom_montage, head_size=None),
            (
                "# ASA electrode file\nReferenceLabel  avg\nUnitPosition    mm\n"
                "NumberPositions=    68\n"
                "Positions\n"
                "-86.0761 -19.9897 -47.9860\n"
                "85.7939 -20.0093 -48.0310\n"
                "0.0083 86.8110 -39.9830\n"
                "-86.0761 -24.9897 -67.9860\n"
                "Labels\nLPA\nRPA\nNz\nDummy\n"
            ),
            make_dig_montage(
                ch_pos={
                    "Dummy": [-0.0860761, -0.0249897, -0.067986],
                },
                nasion=[8.3000e-06, 8.6811e-02, -3.9983e-02],
                lpa=[-0.0860761, -0.0199897, -0.047986],
                rpa=[0.0857939, -0.0200093, -0.048031],
            ),
            "elc",
            None,
            id="old ASA electrode (elc)",
        ),
        pytest.param(
            partial(read_custom_montage, head_size=None),
            (
                "NumberPositions= 96\n"
                "UnitPosition mm\n"
                "Positions\n"
                "E01	:	5.288	-3.658	119.693\n"
                "E02	:	59.518	-4.031	101.404\n"
                "E03	:	29.949	-50.988	98.145\n"
                "Labels\n"
                "E01	E02	E03\n"
            ),
            make_dig_montage(
                ch_pos={
                    "E01": [0.005288, -0.003658, 0.119693],
                    "E02": [0.059518, -0.004031, 0.101404],
                    "E03": [0.029949, -0.050988, 0.098145],
                },
            ),
            "elc",
            None,
            id="new ASA electrode (elc)",
        ),
        pytest.param(
            partial(read_custom_montage, head_size=None),
            (
                "ReferenceLabel\n"
                "avg\n"
                "UnitPosition	mm\n"
                "NumberPositions=	6\n"
                "Positions\n"
                "-69.2574 10.5895 -25.0009\n"
                "3.3791 94.6594 32.2592\n"
                "77.2856 12.0537 -30.2488\n"
                "4.6147 121.8858 8.6370\n"
                "-31.3669 54.0269 94.9191\n"
                "-8.7495 56.5653 99.6655\n"
                "Labels\n"
                "LPA\n"
                "Nz\n"
                "RPA\n"
                "EEG 000\n"
                "EEG 001\n"
                "EEG 002\n"
            ),
            make_dig_montage(
                ch_pos={
                    "EEG 000": [0.004615, 0.121886, 0.008637],
                    "EEG 001": [-0.031367, 0.054027, 0.094919],
                    "EEG 002": [-0.00875, 0.056565, 0.099665],
                },
                nasion=[0.003379, 0.094659, 0.032259],
                lpa=[-0.069257, 0.010589, -0.025001],
                rpa=[0.077286, 0.012054, -0.030249],
            ),
            "elc",
            None,
            id="another old ASA electrode (elc)",
        ),
        pytest.param(
            partial(read_custom_montage, head_size=1),
            (
                "Site  Theta  Phi\n"
                "Fp1  -92    -72\n"
                "Fp2   92     72\n"
                "very_very_very_long_name       -92     72\n"
                "O2        92    -90\n"
            ),
            make_dig_montage(
                ch_pos={
                    "Fp1": [-0.30882875, 0.95047716, -0.0348995],
                    "Fp2": [0.30882875, 0.95047716, -0.0348995],
                    "very_very_very_long_name": [
                        -0.30882875,
                        -0.95047716,
                        -0.0348995,
                    ],  # noqa
                    "O2": [6.11950389e-17, -9.99390827e-01, -3.48994967e-02],
                },
                nasion=None,
                lpa=None,
                rpa=None,
            ),
            "txt",
            None,
            id="generic theta-phi (txt)",
        ),
        pytest.param(
            partial(read_custom_montage, head_size=None),
            (
                "FID\t      LPA\t -120.03\t      0\t      85\n"
                "FID\t      RPA\t  120.03\t      0\t      85\n"
                "FID\t      Nz\t   114.03\t     90\t      85\n"
                "EEG\t      F3\t  -62.027\t -50.053\t     85\n"
                "EEG\t      Fz\t   45.608\t      90\t     85\n"
                "EEG\t      F4\t    62.01\t  50.103\t     85\n"
                "EEG\t      FCz\t   68.01\t  58.103\t     85\n"
            ),
            make_dig_montage(
                ch_pos={
                    "F3": [-0.48200427, 0.57551063, 0.39869712],
                    "Fz": [3.71915931e-17, 6.07384809e-01, 5.94629038e-01],
                    "F4": [0.48142596, 0.57584026, 0.39891983],
                    "FCz": [0.41645989, 0.66914889, 0.31827805],
                },
                nasion=[4.75366562e-17, 7.76332511e-01, -3.46132681e-01],
                lpa=[-7.35898963e-01, 9.01216309e-17, -4.25385374e-01],
                rpa=[0.73589896, 0.0, -0.42538537],
            ),
            "elp",
            None,
            id="BESA spherical model",
        ),
        pytest.param(
            partial(read_dig_hpts, unit="m"),
            ("eeg Fp1 -95.0 -3. -3.\neeg AF7 -1 -1 -3\neeg A3 -2 -2 2\neeg A 0 0 0"),
            make_dig_montage(
                ch_pos={
                    "A": [0.0, 0.0, 0.0],
                    "A3": [-2.0, -2.0, 2.0],
                    "AF7": [-1.0, -1.0, -3.0],
                    "Fp1": [-95.0, -3.0, -3.0],
                },
                nasion=None,
                lpa=None,
                rpa=None,
            ),
            "hpts",
            None,
            id="legacy mne-c",
        ),
        pytest.param(
            read_custom_montage,
            (
                "ch_name, x, y, z\n"
                "Fp1, -95.0, -3., -3.\n"
                "AF7, -1, -1, -3\n"
                "A3, -2, -2, 2\n"
                "A, 0, 0, 0"
            ),
            make_dig_montage(
                ch_pos={
                    "A": [0.0, 0.0, 0.0],
                    "A3": [-2.0, -2.0, 2.0],
                    "AF7": [-1.0, -1.0, -3.0],
                    "Fp1": [-95.0, -3.0, -3.0],
                },
                nasion=None,
                lpa=None,
                rpa=None,
            ),
            "csv",
            None,
            id="CSV file",
        ),
        pytest.param(
            read_custom_montage,
            (
                "1\t-95.0\t-3.\t-3.\tFp1\n"
                "2\t-1\t-1\t-3\tAF7\n"
                "3\t-2\t-2\t2\tA3\n"
                "4\t0\t0\t0\tA"
            ),
            make_dig_montage(
                ch_pos={
                    "A": [0.0, 0.0, 0.0],
                    "A3": [-2.0, -2.0, 2.0],
                    "AF7": [-1.0, -1.0, -3.0],
                    "Fp1": [-95.0, -3.0, -3.0],
                },
                nasion=None,
                lpa=None,
                rpa=None,
            ),
            "xyz",
            None,
            id="XYZ file",
        ),
        pytest.param(
            read_custom_montage,
            (
                "ch_name\tx\ty\tz\n"
                "Fp1\t-95.0\t-3.\t-3.\n"
                "AF7\t-1\t-1\t-3\n"
                "A3\t-2\t-2\t2\n"
                "A\t0\t0\t0"
            ),
            make_dig_montage(
                ch_pos={
                    "A": [0.0, 0.0, 0.0],
                    "A3": [-2.0, -2.0, 2.0],
                    "AF7": [-1.0, -1.0, -3.0],
                    "Fp1": [-95.0, -3.0, -3.0],
                },
                nasion=None,
                lpa=None,
                rpa=None,
            ),
            "tsv",
            None,
            id="TSV file",
        ),
        pytest.param(
            partial(read_custom_montage, head_size=None),
            (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                "<!-- Generated by EasyCap Configurator 19.05.2014 -->\n"
                '<Electrodes defaults="false">\n'
                "  <Electrode>\n"
                "    <Name>Fp1</Name>\n"
                "    <Theta>-90</Theta>\n"
                "    <Phi>-72</Phi>\n"
                "    <Radius>1</Radius>\n"
                "    <Number>1</Number>\n"
                "  </Electrode>\n"
                "  <Electrode>\n"
                "    <Name>Fz</Name>\n"
                "    <Theta>45</Theta>\n"
                "    <Phi>90</Phi>\n"
                "    <Radius>1</Radius>\n"
                "    <Number>2</Number>\n"
                "  </Electrode>\n"
                "  <Electrode>\n"
                "    <Name>F3</Name>\n"
                "    <Theta>-60</Theta>\n"
                "    <Phi>-51</Phi>\n"
                "    <Radius>1</Radius>\n"
                "    <Number>3</Number>\n"
                "  </Electrode>\n"
                "  <Electrode>\n"
                "    <Name>F7</Name>\n"
                "    <Theta>-90</Theta>\n"
                "    <Phi>-36</Phi>\n"
                "    <Radius>1</Radius>\n"
                "    <Number>4</Number>\n"
                "  </Electrode>\n"
                "</Electrodes>"
            ),
            make_dig_montage(
                ch_pos={
                    "Fp1": [-3.09016994e-01, 9.51056516e-01, 6.12323400e-17],
                    "Fz": [4.32978028e-17, 7.07106781e-01, 7.07106781e-01],
                    "F3": [-0.54500745, 0.67302815, 0.5],
                    "F7": [-8.09016994e-01, 5.87785252e-01, 6.12323400e-17],
                },
                nasion=None,
                lpa=None,
                rpa=None,
            ),
            "bvef",
            None,
            id="brainvision",
        ),
    ],
)
def test_montage_readers(reader, file_content, expected_dig, ext, warning, tmp_path):
    """Test that we have an equivalent of read_montage for all file formats."""
    if file_content.startswith("<?xml"):
        pytest.importorskip("defusedxml")
    fname = tmp_path / f"test.{ext}"
    with open(fname, "w") as fid:
        fid.write(file_content)

    if warning is None:
        ctx = nullcontext()
    else:
        ctx = pytest.warns(warning[0], match=warning[1])
    with ctx:
        dig_montage = reader(fname)
    assert isinstance(dig_montage, DigMontage)

    actual_ch_pos = dig_montage._get_ch_pos()
    expected_ch_pos = expected_dig._get_ch_pos()
    for kk in actual_ch_pos:
        assert_allclose(actual_ch_pos[kk], expected_ch_pos[kk], atol=1e-5, err_msg=kk)
    assert len(dig_montage.dig) == len(expected_dig.dig)
    for key in ("nasion", "lpa", "rpa"):
        expected = [
            d
            for d in expected_dig.dig
            if d["kind"] == FIFF.FIFFV_POINT_CARDINAL
            and d["ident"] == getattr(FIFF, f"FIFFV_POINT_{key.upper()}")
        ]
        got = [
            d
            for d in dig_montage.dig
            if d["kind"] == FIFF.FIFFV_POINT_CARDINAL
            and d["ident"] == getattr(FIFF, f"FIFFV_POINT_{key.upper()}")
        ]
        assert len(expected) in (0, 1), key
        assert len(got) in (0, 1), key
        assert len(expected) == len(got)
        if len(expected):
            assert_allclose(got[0]["r"], expected[0]["r"], atol=1e-5, err_msg=key)
    for d1, d2 in zip(dig_montage.dig, expected_dig.dig):
        assert d1["coord_frame"] == d2["coord_frame"]
        for key in ("coord_frame", "ident", "kind"):
            assert isinstance(d1[key], int)
            assert isinstance(d2[key], int)
    with _record_warnings() as w:
        xform = compute_native_head_t(dig_montage)
    assert xform["to"] == FIFF.FIFFV_COORD_HEAD
    assert xform["from"] == FIFF.FIFFV_COORD_UNKNOWN
    n = int(np.allclose(xform["trans"], np.eye(4)))
    assert len(w) == n


@testing.requires_testing_data
def test_read_locs():
    """Test reading EEGLAB locs."""
    data = read_custom_montage(locs_montage_fname)._get_ch_pos()
    assert_allclose(
        actual=np.stack(
            [data[kk] for kk in ("FPz", "EOG1", "F3", "Fz")]  # 4 random chs
        ),
        desired=[
            [0.0, 0.094979, -0.001996],
            [0.02933, 0.069097, -0.058226],
            [-0.053871, 0.064321, 0.044561],
            [0.0, 0.067885, 0.066458],
        ],
        atol=1e-6,
    )


def test_read_dig_dat(tmp_path):
    """Test reading *.dat electrode locations."""
    rows = [
        ["Nasion", 78, 0.00, 1.00, 0.00],
        ["Left", 76, -1.00, 0.00, 0.00],
        ["Right", 82, 1.00, -0.00, 0.00],
        ["O2", 69, -0.50, -0.90, 0.05],
        ["O2", 68, 0.00, 0.01, 0.02],
        ["Centroid", 67, 0.00, 0.00, 0.00],
    ]
    # write mock test.dat file
    fname_temp = tmp_path / "test.dat"
    with open(fname_temp, "w") as fid:
        for row in rows:
            name = row[0].rjust(10)
            data = "\t".join(map(str, row[1:]))
            fid.write(f"{name}\t{data}\n")
    # construct expected value
    idents = {
        78: FIFF.FIFFV_POINT_NASION,
        76: FIFF.FIFFV_POINT_LPA,
        82: FIFF.FIFFV_POINT_RPA,
        68: 1,
        69: 1,
    }
    kinds = {
        78: FIFF.FIFFV_POINT_CARDINAL,
        76: FIFF.FIFFV_POINT_CARDINAL,
        82: FIFF.FIFFV_POINT_CARDINAL,
        69: FIFF.FIFFV_POINT_EEG,
        68: FIFF.FIFFV_POINT_EEG,
    }
    target = {
        row[0]: {
            "r": row[2:],
            "ident": idents[row[1]],
            "kind": kinds[row[1]],
            "coord_frame": 0,
        }
        for row in rows[:-1]
    }
    assert_allclose(target["O2"]["r"], [0, 0.01, 0.02])
    # read it
    with pytest.warns(RuntimeWarning, match=r"Duplic.*for O2 \(2\)"):
        dig = read_dig_dat(fname_temp)
    assert set(dig.ch_names) == {"O2"}
    keys = chain(["Left", "Nasion", "Right"], dig.ch_names)
    target = [target[k] for k in keys]
    assert dig.dig == target


def test_read_dig_montage_using_polhemus_fastscan():
    """Test FastScan."""
    N_EEG_CH = 10
    my_electrode_positions = read_polhemus_fastscan(kit_dir / "test_elp.txt")
    montage = make_dig_montage(
        # EEG_CH
        ch_pos=dict(
            zip(ascii_lowercase[:N_EEG_CH], np.random.RandomState(0).rand(N_EEG_CH, 3))
        ),
        # NO NAMED points
        nasion=my_electrode_positions[0],
        lpa=my_electrode_positions[1],
        rpa=my_electrode_positions[2],
        hpi=my_electrode_positions[3:],
        hsp=read_polhemus_fastscan(kit_dir / "test_hsp.txt"),
        # Other defaults
        coord_frame="unknown",
    )

    assert repr(montage) == (
        "<DigMontage | 500 extras (headshape), 5 HPIs, 3 fiducials, 10 channels>"
    )

    assert set([d["coord_frame"] for d in montage.dig]) == {FIFF.FIFFV_COORD_UNKNOWN}

    EXPECTED_FID_IN_POLHEMUS = {
        "nasion": [0.001393, 0.0131613, -0.0046967],
        "lpa": [-0.0624997, -0.0737271, 0.07996],
        "rpa": [-0.0748957, 0.0873785, 0.0811943],
    }
    fiducials, fid_coordframe = _get_fid_coords(montage.dig)
    assert fid_coordframe == FIFF.FIFFV_COORD_UNKNOWN
    for kk, val in fiducials.items():
        assert_allclose(val, EXPECTED_FID_IN_POLHEMUS[kk])


def test_read_dig_montage_using_polhemus_fastscan_error_handling(tmp_path):
    """Test reading Polhemus FastSCAN errors."""
    with open(kit_dir / "test_elp.txt") as fid:
        content = fid.read().replace("FastSCAN", "XxxxXXXX")

    fname = tmp_path / "faulty_FastSCAN.txt"
    with open(fname, "w") as fid:
        fid.write(content)

    with pytest.raises(ValueError, match="not contain.*Polhemus FastSCAN"):
        _ = read_polhemus_fastscan(fname)

    fname = tmp_path / "faulty_FastSCAN.bar"
    with open(fname, "w") as fid:
        fid.write(content)
    EXPECTED_ERR_MSG = "allowed value is '.txt', but got '.bar' instead"
    with pytest.raises(ValueError, match=EXPECTED_ERR_MSG):
        _ = read_polhemus_fastscan(fname=fname)


def test_read_dig_polhemus_isotrak_hsp():
    """Test reading Polhemus IsoTrak HSP file."""
    EXPECTED_FID_IN_POLHEMUS = {
        "nasion": np.array([1.1056e-01, -5.4210e-19, 0]),
        "lpa": np.array([-2.1075e-04, 8.0793e-02, -7.5894e-19]),
        "rpa": np.array([2.1075e-04, -8.0793e-02, -2.8731e-18]),
    }
    montage = read_dig_polhemus_isotrak(fname=kit_dir / "test.hsp", ch_names=None)
    assert repr(montage) == (
        "<DigMontage | 500 extras (headshape), 0 HPIs, 3 fiducials, 0 channels>"
    )

    fiducials, fid_coordframe = _get_fid_coords(montage.dig)

    assert fid_coordframe == FIFF.FIFFV_COORD_UNKNOWN
    for kk, val in fiducials.items():
        assert_array_equal(val, EXPECTED_FID_IN_POLHEMUS[kk])


def test_read_dig_polhemus_isotrak_elp():
    """Test reading Polhemus IsoTrak ELP file."""
    EXPECTED_FID_IN_POLHEMUS = {
        "nasion": np.array([1.1056e-01, -5.4210e-19, 0]),
        "lpa": np.array([-2.1075e-04, 8.0793e-02, -7.5894e-19]),
        "rpa": np.array([2.1075e-04, -8.0793e-02, -2.8731e-18]),
    }
    montage = read_dig_polhemus_isotrak(fname=kit_dir / "test.elp", ch_names=None)
    assert repr(montage) == (
        "<DigMontage | 0 extras (headshape), 5 HPIs, 3 fiducials, 0 channels>"
    )
    fiducials, fid_coordframe = _get_fid_coords(montage.dig)

    assert fid_coordframe == FIFF.FIFFV_COORD_UNKNOWN
    for kk, val in fiducials.items():
        assert_array_equal(val, EXPECTED_FID_IN_POLHEMUS[kk])


@pytest.fixture(scope="module")
def isotrak_eeg(tmp_path_factory):
    """Mock isotrak file with EEG positions."""
    _SEED = 42
    N_ROWS, N_COLS = 5, 3
    content = np.random.RandomState(_SEED).randn(N_ROWS, N_COLS)

    fname = tmp_path_factory.mktemp("data") / "test.eeg"
    with open(str(fname), "w") as fid:
        fid.write(
            "3	200\n"
            "//Shape file\n"
            "//Minor revision number\n"
            "2\n"
            "//Subject Name\n"
            "%N	Name    \n"
            "////Shape code, number of digitized points\n"
        )
        fid.write(f"0 {N_ROWS:d}\n")
        fid.write(
            "//Position of fiducials X+, Y+, Y- on the subject\n"
            "%F	0.11056	-5.421e-19	0	\n"
            "%F	-0.00021075	0.080793	-7.5894e-19	\n"
            "%F	0.00021075	-0.080793	-2.8731e-18	\n"
            "//No of rows, no of columns; position of digitized points\n"
        )
        fid.write(f"{N_ROWS} {N_COLS}\n")
        for row in content:
            fid.write("\t".join(f"{cell:0.18e}" for cell in row) + "\n")

    return str(fname)


def test_read_dig_polhemus_isotrak_eeg(isotrak_eeg):
    """Test reading Polhemus IsoTrak EEG positions."""
    N_CHANNELS = 5
    _SEED = 42
    EXPECTED_FID_IN_POLHEMUS = {
        "nasion": np.array([1.1056e-01, -5.4210e-19, 0]),
        "lpa": np.array([-2.1075e-04, 8.0793e-02, -7.5894e-19]),
        "rpa": np.array([2.1075e-04, -8.0793e-02, -2.8731e-18]),
    }
    ch_names = [f"eeg {ii:01d}" for ii in range(N_CHANNELS)]
    EXPECTED_CH_POS = dict(
        zip(ch_names, np.random.RandomState(_SEED).randn(N_CHANNELS, 3))
    )

    montage = read_dig_polhemus_isotrak(fname=isotrak_eeg, ch_names=ch_names)
    assert repr(montage) == (
        "<DigMontage | 0 extras (headshape), 0 HPIs, 3 fiducials, 5 channels>"
    )

    fiducials, fid_coordframe = _get_fid_coords(montage.dig)

    assert fid_coordframe == FIFF.FIFFV_COORD_UNKNOWN
    for kk, val in fiducials.items():
        assert_array_equal(val, EXPECTED_FID_IN_POLHEMUS[kk])

    for kk, dig_point in zip(montage.ch_names, _get_dig_eeg(montage.dig)):
        assert_array_equal(dig_point["r"], EXPECTED_CH_POS[kk])
        assert dig_point["coord_frame"] == FIFF.FIFFV_COORD_UNKNOWN


def test_read_dig_polhemus_isotrak_error_handling(isotrak_eeg, tmp_path):
    """Test errors in reading Polhemus IsoTrak files.

    1 - matching ch_names and number of points in isotrak file.
    2 - error for unsupported file extensions.
    """
    # Check ch_names
    N_CHANNELS = 5
    EXPECTED_ERR_MSG = "not match the number of points.*Expected.*5, given 47"
    with pytest.raises(ValueError, match=EXPECTED_ERR_MSG):
        _ = read_dig_polhemus_isotrak(
            fname=isotrak_eeg,
            ch_names=[f"eeg {ii:01d}" for ii in range(N_CHANNELS + 42)],
        )

    # Check fname extensions
    fname = tmp_path / "test.bar"
    shutil.copyfile(isotrak_eeg, fname)

    with pytest.raises(
        ValueError,
        match="Allowed val.*'.hsp', '.elp', and '.eeg', but got '.bar' instead",
    ):
        _ = read_dig_polhemus_isotrak(fname=fname, ch_names=None)


def test_combining_digmontage_objects():
    """Test combining different DigMontage objects."""
    rng = np.random.RandomState(0)
    fiducials = dict(zip(("nasion", "lpa", "rpa"), rng.rand(3, 3)))

    # hsp positions are [1X, 1X, 1X]
    hsp1 = make_dig_montage(**fiducials, hsp=np.full((2, 3), 11.0))
    hsp2 = make_dig_montage(**fiducials, hsp=np.full((2, 3), 12.0))
    hsp3 = make_dig_montage(**fiducials, hsp=np.full((2, 3), 13.0))

    # hpi positions are [2X, 2X, 2X]
    hpi1 = make_dig_montage(**fiducials, hpi=np.full((2, 3), 21.0))
    hpi2 = make_dig_montage(**fiducials, hpi=np.full((2, 3), 22.0))
    hpi3 = make_dig_montage(**fiducials, hpi=np.full((2, 3), 23.0))

    # channels have positions at 40s, 50s, and 60s.
    ch_pos1 = make_dig_montage(
        **fiducials, ch_pos={"h": [41, 41, 41], "b": [42, 42, 42], "g": [43, 43, 43]}
    )
    ch_pos2 = make_dig_montage(
        **fiducials, ch_pos={"n": [51, 51, 51], "y": [52, 52, 52], "p": [53, 53, 53]}
    )
    ch_pos3 = make_dig_montage(
        **fiducials, ch_pos={"v": [61, 61, 61], "a": [62, 62, 62], "l": [63, 63, 63]}
    )

    montage = (
        DigMontage()
        + hsp1
        + hsp2
        + hsp3
        + hpi1
        + hpi2
        + hpi3
        + ch_pos1
        + ch_pos2
        + ch_pos3
    )
    assert repr(montage) == (
        "<DigMontage | 6 extras (headshape), 6 HPIs, 3 fiducials, 9 channels>"
    )

    EXPECTED_MONTAGE = make_dig_montage(
        **fiducials,
        hsp=np.concatenate(
            [np.full((2, 3), 11.0), np.full((2, 3), 12.0), np.full((2, 3), 13.0)]
        ),
        hpi=np.concatenate(
            [np.full((2, 3), 21.0), np.full((2, 3), 22.0), np.full((2, 3), 23.0)]
        ),
        ch_pos={
            "h": [41, 41, 41],
            "b": [42, 42, 42],
            "g": [43, 43, 43],
            "n": [51, 51, 51],
            "y": [52, 52, 52],
            "p": [53, 53, 53],
            "v": [61, 61, 61],
            "a": [62, 62, 62],
            "l": [63, 63, 63],
        },
    )

    # Do some checks to ensure they are the same DigMontage
    assert len(montage.ch_names) == len(EXPECTED_MONTAGE.ch_names)
    assert all([c in montage.ch_names for c in EXPECTED_MONTAGE.ch_names])
    actual_occurrences = _count_points_by_type(montage.dig)
    expected_occurrences = _count_points_by_type(EXPECTED_MONTAGE.dig)
    assert actual_occurrences == expected_occurrences


def test_combining_digmontage_forbiden_behaviors():
    """Test combining different DigMontage objects with repeated names."""
    rng = np.random.RandomState(0)
    fiducials = dict(zip(("nasion", "lpa", "rpa"), rng.rand(3, 3)))
    dig1 = make_dig_montage(
        **fiducials,
        ch_pos=dict(zip(list("abc"), rng.rand(3, 3))),
    )
    dig2 = make_dig_montage(
        **fiducials,
        ch_pos=dict(zip(list("bcd"), rng.rand(3, 3))),
    )
    dig2_wrong_fid = make_dig_montage(
        nasion=rng.rand(3),
        lpa=rng.rand(3),
        rpa=rng.rand(3),
        ch_pos=dict(zip(list("ghi"), rng.rand(3, 3))),
    )
    dig2_wrong_coordframe = make_dig_montage(
        **fiducials, ch_pos=dict(zip(list("ghi"), rng.rand(3, 3))), coord_frame="meg"
    )

    EXPECTED_ERR_MSG = "Cannot.*duplicated channel.*found: 'b', 'c'."
    with pytest.raises(RuntimeError, match=EXPECTED_ERR_MSG):
        _ = dig1 + dig2

    with pytest.raises(RuntimeError, match="fiducial locations do not match"):
        _ = dig1 + dig2_wrong_fid

    with pytest.raises(RuntimeError, match="not in the same coordinate "):
        _ = dig1 + dig2_wrong_coordframe


def test_set_dig_montage():
    """Test setting DigMontage with toy understandable points."""
    N_CHANNELS, N_HSP, N_HPI = 3, 2, 1
    ch_names = list(ascii_lowercase[:N_CHANNELS])
    ch_pos = dict(
        zip(
            ch_names,
            np.arange(N_CHANNELS * 3).reshape(N_CHANNELS, 3),
        )
    )

    montage_ch_only = make_dig_montage(ch_pos=ch_pos, coord_frame="head")

    assert repr(montage_ch_only) == (
        "<DigMontage | 0 extras (headshape), 0 HPIs, 0 fiducials, 3 channels>"
    )
    info = create_info(ch_names, sfreq=1, ch_types="eeg")
    info.set_montage(montage_ch_only)
    assert len(info["dig"]) == len(montage_ch_only.dig) + 3  # added fiducials

    assert_allclose(
        actual=np.array([ch["loc"][:6] for ch in info["chs"]]),
        desired=[
            [0.0, 1.0, 2.0, 0.0, 0.0, 0.0],
            [3.0, 4.0, 5.0, 0.0, 0.0, 0.0],
            [6.0, 7.0, 8.0, 0.0, 0.0, 0.0],
        ],
    )

    montage_full = make_dig_montage(
        ch_pos=dict(**ch_pos, EEG000=np.full(3, 42)),  # 4 = 3 egg + 1 eeg_ref
        nasion=[1, 1, 1],
        lpa=[2, 2, 2],
        rpa=[3, 3, 3],
        hsp=np.full((N_HSP, 3), 4),
        hpi=np.full((N_HPI, 3), 4),
        coord_frame="head",
    )

    assert repr(montage_full) == (
        "<DigMontage | 2 extras (headshape), 1 HPIs, 3 fiducials, 4 channels>"
    )

    info = create_info(ch_names, sfreq=1, ch_types="eeg")
    info.set_montage(montage_full)
    EXPECTED_LEN = sum({"hsp": 2, "hpi": 1, "fid": 3, "eeg": 4}.values())
    assert len(info["dig"]) == EXPECTED_LEN
    assert_allclose(
        actual=np.array([ch["loc"][:6] for ch in info["chs"]]),
        desired=[
            [0.0, 1.0, 2.0, 42.0, 42.0, 42.0],
            [3.0, 4.0, 5.0, 42.0, 42.0, 42.0],
            [6.0, 7.0, 8.0, 42.0, 42.0, 42.0],
        ],
    )


def test_set_dig_montage_with_nan_positions():
    """Test that fiducials are not NaN.

    Test that setting a montage with some NaN positions does not produce
    NaN fiducials.
    """

    def _ensure_fid_not_nan(info, ch_pos):
        montage_kwargs = dict(ch_pos=dict(), coord_frame="head")
        for ch_idx, ch in enumerate(info.ch_names):
            montage_kwargs["ch_pos"][ch] = ch_pos[ch_idx]

        new_montage = make_dig_montage(**montage_kwargs)
        info = info.copy()
        info.set_montage(new_montage)

        recovered_montage = info.get_montage()
        fid_coords, coord_frame = _get_fid_coords(
            recovered_montage.dig, raise_error=False
        )

        for fid_coord in fid_coords.values():
            if fid_coord is not None:
                assert not np.isnan(fid_coord).any()

        return fid_coords, coord_frame

    channels = list("ABCDEF")
    info = create_info(channels, 1000, ch_types="seeg")

    # if all positions are NaN, the fiducials should not be NaN, but None
    ch_pos = [info["chs"][ch_idx]["loc"][:3] for ch_idx in range(len(channels))]
    fid_coords, coord_frame = _ensure_fid_not_nan(info, ch_pos)
    for fid_coord in fid_coords.values():
        assert fid_coord is None
    assert coord_frame is None

    # if some positions are not NaN, the fiducials should be a non-NaN array
    ch_pos[0] = np.array([1.0, 1.5, 1.0])
    ch_pos[1] = np.array([2.0, 1.5, 1.5])
    ch_pos[2] = np.array([1.25, 1.0, 1.25])
    fid_coords, coord_frame = _ensure_fid_not_nan(info, ch_pos)
    for fid_coord in fid_coords.values():
        assert isinstance(fid_coord, np.ndarray)
    assert coord_frame == FIFF.FIFFV_COORD_HEAD


@testing.requires_testing_data
def test_fif_dig_montage(tmp_path, monkeypatch):
    """Test FIF dig montage support."""
    dig_montage = read_dig_fif(fif_dig_montage_fname, verbose="error")

    # test round-trip IO
    fname_temp = tmp_path / "test-dig.fif"
    _check_roundtrip(dig_montage, fname_temp)

    # Make a BrainVision file like the one the user would have had
    raw_bv = read_raw_brainvision(bv_fname, preload=True)
    raw_bv_2 = raw_bv.copy()
    mapping = dict()
    for ii, ch_name in enumerate(raw_bv.ch_names, 1):
        mapping[ch_name] = f"EEG{ii:03d}"
    raw_bv.rename_channels(mapping)
    for ii, ch_name in enumerate(raw_bv_2.ch_names, 33):
        mapping[ch_name] = f"EEG{ii:03d}"
    raw_bv_2.rename_channels(mapping)
    raw_bv.add_channels([raw_bv_2])
    for ch in raw_bv.info["chs"]:
        ch["kind"] = FIFF.FIFFV_EEG_CH

    # Set the montage
    raw_bv.set_montage(dig_montage)

    # Check the result
    evoked = read_evokeds(evoked_fname)[0]

    # check info[chs] matches
    assert_equal(len(raw_bv.ch_names), len(evoked.ch_names) - 1)
    for ch_py, ch_c in zip(raw_bv.info["chs"], evoked.info["chs"][:-1]):
        assert_equal(ch_py["ch_name"], ch_c["ch_name"].replace("EEG ", "EEG"))
        # C actually says it's unknown, but it's not (?):
        # assert_equal(ch_py['coord_frame'], ch_c['coord_frame'])
        assert_equal(ch_py["coord_frame"], FIFF.FIFFV_COORD_HEAD)
        c_loc = ch_c["loc"].copy()
        c_loc[c_loc == 0] = np.nan
        assert_allclose(ch_py["loc"], c_loc, atol=1e-7)

    # check info[dig]
    assert_dig_allclose(raw_bv.info, evoked.info)

    # Roundtrip of non-FIF start
    montage = make_dig_montage(hsp=read_polhemus_fastscan(hsp), hpi=read_mrk(hpi))
    elp_points = read_polhemus_fastscan(elp)
    ch_pos = {f"ECoG{k:03d}": pos for k, pos in enumerate(elp_points[3:], 1)}
    assert len(elp_points) == 8  # there are only 8 but pretend the last are ECoG
    other = make_dig_montage(
        nasion=elp_points[0], lpa=elp_points[1], rpa=elp_points[2], ch_pos=ch_pos
    )
    assert other.ch_names[0].startswith("ECoG")
    montage += other
    assert montage.ch_names[0].startswith("ECoG")
    _check_roundtrip(montage, fname_temp, "unknown")
    montage = transform_to_head(montage)
    _check_roundtrip(montage, fname_temp)
    montage.dig[0]["coord_frame"] = FIFF.FIFFV_COORD_UNKNOWN
    with pytest.raises(RuntimeError, match="Only a single coordinate"):
        montage.save(fname_temp, overwrite=True)
    montage.dig[0]["coord_frame"] = FIFF.FIFFV_COORD_HEAD

    # Check that old-style files can be read, too, using EEG001 etc.
    def write_dig_no_ch_names(*args, **kwargs):
        kwargs["ch_names"] = None
        return write_dig(*args, **kwargs)

    monkeypatch.setattr(mne.channels.montage, "write_dig", write_dig_no_ch_names)
    montage.save(fname_temp, overwrite=True)
    montage_read = read_dig_fif(fname_temp)
    default_ch_names = [f"EEG{ii:03d}" for ii in range(1, 6)]
    assert montage_read.ch_names == default_ch_names


@testing.requires_testing_data
def test_egi_dig_montage(tmp_path):
    """Test EGI MFF XML dig montage support."""
    pytest.importorskip("defusedxml")
    dig_montage = read_dig_egi(egi_dig_montage_fname)
    fid, coord = _get_fid_coords(dig_montage.dig)

    assert coord == FIFF.FIFFV_COORD_UNKNOWN
    assert_allclose(
        actual=np.array([fid[key] for key in ["nasion", "lpa", "rpa"]]),
        desired=[
            [0.0, 10.564, -2.051],  # noqa
            [-8.592, 0.498, -4.128],  # noqa
            [8.592, 0.498, -4.128],
        ],  # noqa
    )

    # Test accuracy and embedding within raw object
    raw_egi = read_raw_egi(
        egi_raw_fname,
        channel_naming="EEG %03d",
        events_as_annotations=True,
    )

    raw_egi.set_montage(dig_montage)
    test_raw_egi = read_raw_fif(egi_fif_fname)

    assert_equal(len(raw_egi.ch_names), len(test_raw_egi.ch_names))
    for ch_raw, ch_test_raw in zip(raw_egi.info["chs"], test_raw_egi.info["chs"]):
        assert_equal(ch_raw["ch_name"], ch_test_raw["ch_name"])
        assert_equal(ch_raw["coord_frame"], FIFF.FIFFV_COORD_HEAD)
        assert_allclose(ch_raw["loc"], ch_test_raw["loc"], atol=1e-7)

    assert_dig_allclose(raw_egi.info, test_raw_egi.info)

    dig_montage_in_head = transform_to_head(dig_montage.copy())
    fid, coord = _get_fid_coords(dig_montage_in_head.dig)
    assert coord == FIFF.FIFFV_COORD_HEAD
    assert_allclose(
        actual=np.array([fid[key] for key in ["nasion", "lpa", "rpa"]]),
        desired=[[0.0, 10.278, 0.0], [-8.592, 0.0, 0.0], [8.592, 0.0, 0.0]],
        atol=1e-4,
    )

    # test round-trip IO (with GZ)
    fname_temp = tmp_path / "egi_test-dig.fif.gz"
    _check_roundtrip(dig_montage, fname_temp, "unknown")
    _check_roundtrip(dig_montage_in_head, fname_temp)


@testing.requires_testing_data
def test_read_dig_captrak(tmp_path):
    """Test reading a captrak montage file."""
    pytest.importorskip("defusedxml")
    EXPECTED_CH_NAMES_OLD = [
        "AF3",
        "AF4",
        "AF7",
        "AF8",
        "C1",
        "C2",
        "C3",
        "C4",
        "C5",
        "C6",
        "CP1",
        "CP2",
        "CP3",
        "CP4",
        "CP5",
        "CP6",
        "CPz",
        "Cz",
        "F1",
        "F2",
        "F3",
        "F4",
        "F5",
        "F6",
        "F7",
        "F8",
        "FC1",
        "FC2",
        "FC3",
        "FC4",
        "FC5",
        "FC6",
        "FT10",
        "FT7",
        "FT8",
        "FT9",
        "Fp1",
        "Fp2",
        "Fz",
        "GND",
        "O1",
        "O2",
        "Oz",
        "P1",
        "P2",
        "P3",
        "P4",
        "P5",
        "P6",
        "P7",
        "P8",
        "PO10",
        "PO3",
        "PO4",
        "PO7",
        "PO8",
        "PO9",
        "POz",
        "Pz",
        "REF",
        "T7",
        "T8",
        "TP10",
        "TP7",
        "TP8",
        "TP9",
    ]
    EXPECTED_CH_NAMES = [
        "T7",
        "FC5",
        "F7",
        "C5",
        "FT7",
        "FT9",
        "TP7",
        "TP9",
        "P7",
        "CP5",
        "PO7",
        "C3",
        "CP3",
        "P5",
        "P3",
        "PO3",
        "PO9",
        "O1",
        "Oz",
        "POz",
        "O2",
        "PO4",
        "P1",
        "Pz",
        "P2",
        "CP2",
        "CP1",
        "CPz",
        "Cz",
        "C1",
        "FC1",
        "FC3",
        "REF",
        "F3",
        "F1",
        "Fz",
        "F5",
        "AF7",
        "AF3",
        "Fp1",
        "GND",
        "F2",
        "AF4",
        "Fp2",
        "F4",
        "F8",
        "F6",
        "AF8",
        "FC2",
        "FC6",
        "FC4",
        "C2",
        "C4",
        "P4",
        "CP4",
        "PO8",
        "P8",
        "P6",
        "CP6",
        "PO10",
        "TP10",
        "TP8",
        "FT10",
        "T8",
        "C6",
        "FT8",
    ]
    assert set(EXPECTED_CH_NAMES) == set(EXPECTED_CH_NAMES_OLD)
    montage = read_dig_captrak(fname=data_path / "montage" / "captrak_coords.bvct")

    assert montage.ch_names == EXPECTED_CH_NAMES
    assert repr(montage) == (
        "<DigMontage | 0 extras (headshape), 0 HPIs, 3 fiducials, 66 channels>"
    )

    montage = transform_to_head(montage)  # transform_to_head has to be tested
    _check_roundtrip(montage=montage, fname=tmp_path / "bvct_test-dig.fif")

    fid, _ = _get_fid_coords(montage.dig)
    assert_allclose(
        actual=np.array([fid.nasion, fid.lpa, fid.rpa]),
        desired=[[0, 0.11309, 0], [-0.09189, 0, 0], [0.09240, 0, 0]],
        atol=1e-5,
    )

    raw_bv = read_raw_brainvision(bv_raw_fname)
    raw_bv.set_channel_types({"HEOG": "eog", "VEOG": "eog", "ECG": "ecg"})

    raw_bv.set_montage(montage)

    test_raw_bv = read_raw_fif(bv_fif_fname)

    # compare after set_montage using chs loc.
    for actual, expected in zip(raw_bv.info["chs"], test_raw_bv.info["chs"]):
        assert_allclose(actual["loc"][:3], expected["loc"][:3])
        if actual["kind"] == FIFF.FIFFV_EEG_CH:
            assert_allclose(
                actual["loc"][3:6], [-0.005103, 0.05395, 0.144622], rtol=1e-04
            )


# https://gist.github.com/larsoner/2264fb5895070d29a8c9aa7c0dc0e8a6
_MGH60 = (
    "Fp1 Fpz Fp2 "
    "AF7 AF3 AF4 AF8 "
    "F7 F5 F3 F1 Fz F2 F4 F6 F8 "
    "FT9 FT7 FC5 FC1 FC2 FC6 FT8 FT10 "
    "T9 T7 C5 C3 C1 Cz C2 C4 C6 T8 T10 "
    "TP9 TP7 CP3 CP1 CP2 CP4 TP8 TP10 "
    "P7 P5 P3 P1 Pz P2 P4 P6 P8 "
    "PO7 PO3 PO4 PO8 "
    "O1 Oz O2 "
    "Iz"
).split()


@pytest.mark.parametrize("rename", ("raw", "montage", "custom"))
def test_set_montage_mgh(rename):
    """Test setting 'mgh60' montage to old fif."""
    raw = read_raw_fif(fif_fname)
    eeg_picks = pick_types(raw.info, meg=False, eeg=True, exclude=())
    assert list(eeg_picks) == [
        ii for ii, name in enumerate(raw.ch_names) if name.startswith("EEG")
    ]
    orig_pos = np.array([raw.info["chs"][pick]["loc"][:3] for pick in eeg_picks])
    atol = 1e-6
    mon = None
    if rename == "raw":
        raw.rename_channels(lambda x: x.replace("EEG ", "EEG"))
        raw.set_montage("mgh60")  # test loading with string argument
    elif rename == "montage":
        mon = make_standard_montage("mgh60")
        mon.rename_channels(lambda x: x.replace("EEG", "EEG "))
        assert [raw.ch_names[pick] for pick in eeg_picks] == mon.ch_names
        raw.set_montage(mon)
    else:
        atol = 3e-3  # different subsets of channel locations
        assert rename == "custom"
        assert len(_MGH60) == 60
        mon = make_standard_montage("standard_1020")
        assert len(mon._get_ch_pos()) == 94

        def renamer(x):
            try:
                return f"EEG {_MGH60.index(x) + 1:03d}"
            except ValueError:
                return x

        mon = mon.rename_channels(renamer)
        raw.set_montage(mon)

    if mon is not None:
        # first two are 'Fp1' and 'Fz', take them from standard_1020.elc --
        # they should not be changed on load!
        want_pos = [[-29.4367, 83.9171, -6.9900], [0.1123, 88.2470, -1.7130]]
        got_pos = [
            mon.get_positions()["ch_pos"][f"EEG {x:03d}"] * 1000 for x in range(1, 3)
        ]
        assert_allclose(want_pos, got_pos)
        assert mon.dig[0]["coord_frame"] == FIFF.FIFFV_COORD_MRI
        trans = compute_native_head_t(mon)
        trans_2 = _get_trans("fsaverage", "mri", "head")[0]
        assert trans["to"] == trans_2["to"]
        assert trans["from"] == trans_2["from"]
        assert_allclose(trans["trans"], trans_2["trans"], atol=1e-6)

    new_pos = np.array(
        [ch["loc"][:3] for ch in raw.info["chs"] if ch["ch_name"].startswith("EEG")]
    )
    assert (orig_pos != new_pos).all()

    r0 = _fit_sphere(new_pos)[1]
    assert_allclose(r0, [-0.001043, 0.01469, 0.041448], atol=1e-4)
    # spot check: Fp1 and Fpz
    assert_allclose(
        new_pos[:2],
        [[-0.030903, 0.114585, 0.027867], [-0.001337, 0.119102, 0.03289]],
        atol=atol,
    )


@pytest.mark.parametrize(
    "fname, montage, n_eeg, n_good, bads",
    [
        (fif_fname, "mgh60", 60, 59, ["EEG 053"]),
        pytest.param(
            mgh70_fname, "mgh70", 70, 64, None, marks=[testing._pytest_mark()]
        ),
    ],
)
def test_montage_positions_similar(fname, montage, n_eeg, n_good, bads):
    """Test that montages give spatially similar positions."""
    # 1. Prepare data: load, set bads (if missing), and filter
    raw = read_raw_fif(fname).pick(picks="eeg")
    if bads is not None:
        assert raw.info["bads"] == []
        raw.info["bads"] = bads
    assert len(raw.ch_names) == n_eeg
    raw.pick(picks="eeg", exclude="bads").load_data()
    raw.apply_function(lambda x: x - x.mean())  # remove DC
    raw.filter(None, 40)  # remove line noise
    assert len(raw.ch_names) == n_good
    if montage == "mgh60":
        montage = make_standard_montage(montage)
        montage.rename_channels(lambda n: f"EEG {n[-3:]}")
    raw_mon = raw.copy().set_montage(montage)
    # 2. First test: CSDs should be similar (CSD uses 3D positions)
    csd = compute_current_source_density(raw).get_data()
    csd_mon = compute_current_source_density(raw_mon).get_data()
    corr = np.corrcoef(csd.ravel(), csd_mon.ravel())[0, 1]
    assert 0.9 < corr < 0.99, corr
    # 3. Second test: interpolation of some bads should be similar
    bad_picks = np.linspace(0, n_good, 6, endpoint=False).round().astype(int)
    bads = [raw.ch_names[idx] for idx in bad_picks]
    orig_data = raw.get_data(bad_picks)
    assert_allclose(orig_data, raw_mon.get_data(bad_picks))
    raw.info["bads"] = bads
    raw_mon.info["bads"] = bads
    raw.interpolate_bads()
    raw_mon.interpolate_bads()
    orig_data = orig_data.ravel()
    corr = np.corrcoef(orig_data, raw.get_data(bad_picks).ravel())[0, 1]
    assert 0.95 < corr < 0.99, corr
    corr = np.corrcoef(orig_data, raw_mon.get_data(bad_picks).ravel())[0, 1]
    assert 0.95 < corr < 0.99, corr
    # 4. Third test: project each to a sphere, check cosine angles are small
    poss = dict()
    for kind, this_raw in (("orig", raw), ("mon", raw_mon)):
        pos = np.array(
            list(this_raw.get_montage().get_positions()["ch_pos"].values()), float
        )
        pos -= np.mean(pos, axis=0)
        pos /= np.linalg.norm(pos, axis=1, keepdims=True)
        poss[kind] = pos
    ang = np.rad2deg(  # arccos is in [0, pi]
        np.arccos(np.minimum(np.sum(poss["orig"] * poss["mon"], axis=1), 1))
    )
    assert_array_less(ang, 20)  # less than 20 deg
    assert_array_less(0, ang)  # but not equal


def _check_roundtrip(montage, fname, coord_frame="head"):
    """Check roundtrip writing."""
    montage.save(fname, overwrite=True)
    montage_read = read_dig_fif(fname=fname)

    assert repr(montage) == repr(montage_read)
    assert _check_get_coord_frame(montage_read.dig) == coord_frame
    assert_dig_allclose(montage, montage_read)
    assert montage.ch_names == montage_read.ch_names


def test_digmontage_constructor_errors():
    """Test proper error messaging."""
    with pytest.raises(ValueError, match="does not match the number"):
        _ = DigMontage(ch_names=["foo", "bar"], dig=list())


def test_transform_to_head_and_compute_dev_head_t():
    """Test transform_to_head and compute_dev_head_t."""
    EXPECTED_DEV_HEAD_T = [
        [-3.72201691e-02, -9.98212167e-01, -4.67667497e-02, -7.31583414e-04],
        [8.98064989e-01, -5.39382685e-02, 4.36543170e-01, 1.60134431e-02],
        [-4.38285221e-01, -2.57513699e-02, 8.98466990e-01, 6.13035748e-02],
        [0.00000000e00, 0.00000000e00, 0.00000000e00, 1.00000000e00],
    ]

    EXPECTED_FID_IN_POLHEMUS = {
        "nasion": np.array([0.001393, 0.0131613, -0.0046967]),
        "lpa": np.array([-0.0624997, -0.0737271, 0.07996]),
        "rpa": np.array([-0.0748957, 0.0873785, 0.0811943]),
    }

    EXPECTED_FID_IN_HEAD = {
        "nasion": np.array([-8.94466792e-18, 1.10559624e-01, -3.85185989e-34]),
        "lpa": np.array([-8.10816716e-02, 6.56321671e-18, 0]),
        "rpa": np.array([8.05048781e-02, -6.47441364e-18, 0]),
    }

    hpi_dev = np.array(
        [
            [2.13951493e-02, 8.47444056e-02, -5.65431188e-02],  # noqa
            [2.10299433e-02, -8.03141101e-02, -6.34420259e-02],  # noqa
            [1.05916829e-01, 8.18485672e-05, 1.19928083e-02],  # noqa
            [9.26595105e-02, 4.64804385e-02, 8.45141253e-03],  # noqa
            [9.42554419e-02, -4.35206589e-02, 8.78999363e-03],
        ]  # noqa
    )

    hpi_polhemus = np.array(
        [
            [-0.0595004, -0.0704836, 0.075893],  # noqa
            [-0.0646373, 0.0838228, 0.0762123],  # noqa
            [-0.0135035, 0.0072522, -0.0268405],  # noqa
            [-0.0202967, -0.0351498, -0.0129305],  # noqa
            [-0.0277519, 0.0452628, -0.0222407],
        ]  # noqa
    )

    montage_polhemus = make_dig_montage(
        **EXPECTED_FID_IN_POLHEMUS, hpi=hpi_polhemus, coord_frame="unknown"
    )

    montage_meg = make_dig_montage(hpi=hpi_dev, coord_frame="meg")

    # Test regular workflow to get dev_head_t
    montage = montage_polhemus + montage_meg
    fids, _ = _get_fid_coords(montage.dig)
    for kk in fids:
        assert_allclose(fids[kk], EXPECTED_FID_IN_POLHEMUS[kk], atol=1e-5)

    with pytest.raises(ValueError, match="set to head coordinate system"):
        _ = compute_dev_head_t(montage)

    montage = transform_to_head(montage)

    fids, _ = _get_fid_coords(montage.dig)
    for kk in fids:
        assert_allclose(fids[kk], EXPECTED_FID_IN_HEAD[kk], atol=1e-5)

    dev_head_t = compute_dev_head_t(montage)
    assert_allclose(dev_head_t["trans"], EXPECTED_DEV_HEAD_T, atol=5e-7)

    # Test errors when number of HPI points do not match
    EXPECTED_ERR_MSG = "Device-to-Head .*Got 0 .*device and 5 points in head"
    with pytest.raises(ValueError, match=EXPECTED_ERR_MSG):
        _ = compute_dev_head_t(transform_to_head(montage_polhemus))

    EXPECTED_ERR_MSG = "Device-to-Head .*Got 5 .*device and 0 points in head"
    with pytest.raises(ValueError, match=EXPECTED_ERR_MSG):
        _ = compute_dev_head_t(
            transform_to_head(
                montage_meg + make_dig_montage(**EXPECTED_FID_IN_POLHEMUS)
            )
        )

    EXPECTED_ERR_MSG = "Device-to-Head .*Got 3 .*device and 5 points in head"
    with pytest.raises(ValueError, match=EXPECTED_ERR_MSG):
        _ = compute_dev_head_t(
            transform_to_head(
                DigMontage(dig=_format_dig_points(montage_meg.dig[:3]))
                + montage_polhemus
            )
        )


def test_set_montage_with_mismatching_ch_names():
    """Test setting a DigMontage with mismatching ch_names."""
    raw = read_raw_fif(fif_fname)
    montage = make_standard_montage("mgh60")

    # 'EEG 001' and 'EEG001' won't match
    missing_err = "60 channel positions not present"
    with pytest.raises(ValueError, match=missing_err):
        raw.set_montage(montage)

    montage.ch_names = [  # modify the names in place
        name.replace("EEG", "EEG ") for name in montage.ch_names
    ]
    raw.set_montage(montage)  # does not raise

    # Case sensitivity
    raw.rename_channels(lambda x: x.lower())
    with pytest.raises(ValueError, match=missing_err):
        raw.set_montage(montage)
    # should work
    raw.set_montage(montage, match_case=False)
    raw.rename_channels(lambda x: x.upper())  # restore
    assert "EEG 001" in raw.ch_names and "eeg 001" not in raw.ch_names
    raw.rename_channels({"EEG 002": "eeg 001"})
    assert "EEG 001" in raw.ch_names and "eeg 001" in raw.ch_names
    with pytest.warns(RuntimeWarning, match="changed from V to NA"):
        raw.set_channel_types({"eeg 001": "misc"})
    raw.set_montage(montage)
    with pytest.warns(RuntimeWarning, match="changed from NA to V"):
        raw.set_channel_types({"eeg 001": "eeg"})
    with pytest.raises(ValueError, match="1 channel position not present"):
        raw.set_montage(montage)
    with pytest.raises(ValueError, match="match_case=False as 1 channel name"):
        raw.set_montage(montage, match_case=False)
    info = create_info(["EEG 001"], 1000.0, "eeg")
    mon = make_dig_montage(
        {"EEG 001": np.zeros(3), "eeg 001": np.zeros(3)},
        nasion=[0, 1.0, 0],
        rpa=[1.0, 0, 0],
        lpa=[-1.0, 0, 0],
    )
    info.set_montage(mon)
    with pytest.raises(ValueError, match="match_case=False as 1 montage name"):
        info.set_montage(mon, match_case=False)


def test_set_montage_with_sub_super_set_of_ch_names():
    """Test info and montage ch_names matching criteria."""
    N_CHANNELS = len("abcdef")
    montage = _make_toy_dig_montage(N_CHANNELS, coord_frame="head")

    # montage and info match
    info = create_info(ch_names=list("abcdef"), sfreq=1, ch_types="eeg")
    info.set_montage(montage)

    # montage is a SUPERset of info
    info = create_info(list("abc"), sfreq=1, ch_types="eeg")
    info.set_montage(montage)
    assert len(info["dig"]) == len(list("abc")) + 3  # 3 fiducials

    # montage is a SUBset of info
    _MSG = "subset of info. There are 2 .* not present in the DigMontage"
    info = create_info(ch_names=list("abcdfgh"), sfreq=1, ch_types="eeg")
    with pytest.raises(ValueError, match=_MSG) as exc:
        info.set_montage(montage)
    # plus suggestions
    assert exc.match("set_channel_types")
    assert exc.match("on_missing")


def test_set_montage_with_known_aliases():
    """Test matching unrecognized channel locations to known aliases."""
    # montage and info match
    mock_montage_ch_names = ["POO7", "POO8"]
    n_channels = len(mock_montage_ch_names)

    montage = make_dig_montage(
        ch_pos=dict(
            zip(
                mock_montage_ch_names,
                np.arange(n_channels * 3).reshape(n_channels, 3),
            )
        ),
        coord_frame="head",
    )

    mock_info_ch_names = ["Cb1", "Cb2"]
    info = create_info(ch_names=mock_info_ch_names, sfreq=1, ch_types="eeg")
    info.set_montage(montage, match_alias=True)

    # work with match_case
    mock_info_ch_names = ["cb1", "cb2"]
    info = create_info(ch_names=mock_info_ch_names, sfreq=1, ch_types="eeg")
    info.set_montage(montage, match_case=False, match_alias=True)

    # should warn user T1 instead of its alias T9
    mock_info_ch_names = ["Cb1", "T1"]
    info = create_info(ch_names=mock_info_ch_names, sfreq=1, ch_types="eeg")
    with pytest.raises(ValueError, match="T1"):
        info.set_montage(montage, match_case=False, match_alias=True)


def test_heterogeneous_ch_type():
    """Test ch_names matching criteria with heterogeneous ch_type."""
    VALID_MONTAGE_NAMED_CHS = ("eeg", "ecog", "seeg", "dbs")

    montage = _make_toy_dig_montage(
        n_channels=len(VALID_MONTAGE_NAMED_CHS),
        coord_frame="head",
    )

    # Montage and info match
    info = create_info(montage.ch_names, 1.0, list(VALID_MONTAGE_NAMED_CHS))
    RawArray(np.zeros((4, 1)), info, copy=None).set_montage(montage)


def test_set_montage_coord_frame_in_head_vs_unknown():
    """Test set montage using head and unknown only."""
    N_CHANNELS, NaN = 3, np.nan

    raw = _make_toy_raw(N_CHANNELS)
    montage_in_head = _make_toy_dig_montage(N_CHANNELS, coord_frame="head")
    montage_in_unknown = _make_toy_dig_montage(N_CHANNELS, coord_frame="unknown")
    montage_in_unknown_with_fid = _make_toy_dig_montage(
        N_CHANNELS,
        coord_frame="unknown",
        nasion=[0, 1, 0],
        lpa=[1, 0, 0],
        rpa=[-1, 0, 0],
    )

    assert_allclose(
        actual=np.array([ch["loc"] for ch in raw.info["chs"]]),
        desired=np.full((N_CHANNELS, 12), np.nan),
    )

    raw.set_montage(montage_in_head)
    assert_allclose(
        actual=np.array([ch["loc"] for ch in raw.info["chs"]]),
        desired=[
            [0.0, 1.0, 2.0, 0.0, 0.0, 0.0, NaN, NaN, NaN, NaN, NaN, NaN],
            [3.0, 4.0, 5.0, 0.0, 0.0, 0.0, NaN, NaN, NaN, NaN, NaN, NaN],
            [6.0, 7.0, 8.0, 0.0, 0.0, 0.0, NaN, NaN, NaN, NaN, NaN, NaN],
        ],
    )

    with pytest.warns(RuntimeWarning, match="assuming identity"):
        raw.set_montage(montage_in_unknown)

    raw.set_montage(montage_in_unknown_with_fid)
    assert_allclose(
        actual=np.array([ch["loc"] for ch in raw.info["chs"]]),
        desired=[
            [-0.0, 1.0, -2.0, 0.0, 0.0, 0.0, NaN, NaN, NaN, NaN, NaN, NaN],
            [-3.0, 4.0, -5.0, 0.0, 0.0, 0.0, NaN, NaN, NaN, NaN, NaN, NaN],
            [-6.0, 7.0, -8.0, 0.0, 0.0, 0.0, NaN, NaN, NaN, NaN, NaN, NaN],
        ],
    )

    # check no collateral effects from transforming montage
    assert _check_get_coord_frame(montage_in_unknown_with_fid.dig) == "unknown"
    assert_array_equal(
        _get_dig_montage_pos(montage_in_unknown_with_fid),
        [[0, 1, 2], [3, 4, 5], [6, 7, 8]],
    )


@testing.requires_testing_data
@pytest.mark.parametrize("ch_type", ("eeg", "ecog", "seeg", "dbs"))
def test_montage_head_frame(ch_type):
    """Test that head frame is set properly."""
    # gh-9446
    data = np.random.randn(2, 100)
    info = create_info(["a", "b"], 512, ch_type)
    for ch in info["chs"]:
        assert ch["coord_frame"] == FIFF.FIFFV_COORD_HEAD
    raw = RawArray(data, info)
    ch_pos = dict(
        a=[-0.00250136, 0.04913788, 0.05047056], b=[-0.00528394, 0.05066484, 0.05061559]
    )
    lpa, nasion, rpa = get_mni_fiducials("fsaverage", subjects_dir=subjects_dir)
    lpa, nasion, rpa = lpa["r"], nasion["r"], rpa["r"]
    montage = make_dig_montage(
        ch_pos, coord_frame="mri", nasion=nasion, lpa=lpa, rpa=rpa
    )
    mri_head_t = compute_native_head_t(montage)
    raw.set_montage(montage)
    pos = apply_trans(mri_head_t, np.array(list(ch_pos.values())))
    for p, ch in zip(pos, raw.info["chs"]):
        assert ch["coord_frame"] == FIFF.FIFFV_COORD_HEAD
        assert_allclose(p, ch["loc"][:3])

    # Also test that including channels in the montage that will not have their
    # positions set will emit a warning
    with pytest.warns(RuntimeWarning, match="changed from V to NA"):
        raw.set_channel_types(dict(a="misc"))
    with pytest.warns(RuntimeWarning, match="Not setting .*of 1 misc channel"):
        raw.set_montage(montage)

    # and with a bunch of bad types
    raw = read_raw_fif(fif_fname)
    ch_pos = {ch_name: np.zeros(3) for ch_name in raw.ch_names}
    mon = make_dig_montage(ch_pos, coord_frame="head")
    with pytest.warns(RuntimeWarning, match="316 eog/grad/mag/stim channels"):
        raw.set_montage(mon)


def test_set_montage_with_missing_coordinates():
    """Test set montage with missing coordinates."""
    N_CHANNELS, NaN = 3, np.nan

    raw = _make_toy_raw(N_CHANNELS)
    raw.set_channel_types({ch: "ecog" for ch in raw.ch_names})
    # don't include all the channels
    ch_names = raw.ch_names[1:]
    n_channels = len(ch_names)
    ch_coords = np.arange(n_channels * 3).reshape(n_channels, 3)
    montage_in_mri = make_dig_montage(
        ch_pos=dict(
            zip(
                ch_names,
                ch_coords,
            )
        ),
        coord_frame="unknown",
        nasion=[0, 1, 0],
        lpa=[1, 0, 0],
        rpa=[-1, 0, 0],
    )

    with pytest.raises(ValueError, match="DigMontage is only a subset of info"):
        raw.set_montage(montage_in_mri)

    with pytest.raises(ValueError, match="Invalid value"):
        raw.set_montage(montage_in_mri, on_missing="foo")

    with pytest.raises(TypeError, match="must be an instance"):
        raw.set_montage(montage_in_mri, on_missing=True)

    with pytest.warns(RuntimeWarning, match="DigMontage is only a subset of info"):
        raw.set_montage(montage_in_mri, on_missing="warn")

    raw.set_montage(montage_in_mri, on_missing="ignore")
    assert_allclose(
        actual=np.array([ch["loc"] for ch in raw.info["chs"]]),
        desired=[
            [NaN, NaN, NaN, NaN, NaN, NaN, NaN, NaN, NaN, NaN, NaN, NaN],
            [0.0, 1.0, -2.0, 0.0, 0.0, 0.0, NaN, NaN, NaN, NaN, NaN, NaN],
            [-3.0, 4.0, -5.0, 0.0, 0.0, 0.0, NaN, NaN, NaN, NaN, NaN, NaN],
        ],
    )


@testing.requires_testing_data
def test_get_montage():
    """Test get montage from Instance.

    Test with standard montage and then loaded in montage.
    """
    # 1. read in testing data and assert montage roundtrip
    # for testing dataset: 'test_raw.fif'
    raw = read_raw_fif(fif_fname)
    raw = raw.rename_channels(lambda name: name.replace("EEG ", "EEG"))
    raw2 = raw.copy()
    # get montage and then set montage and
    # it should be the same
    montage = raw.get_montage()
    raw.set_montage(montage, on_missing="raise")
    test_montage = raw.get_montage()
    assert_object_equal(raw.info["chs"], raw2.info["chs"])
    assert_dig_allclose(raw2.info, raw.info)
    assert_object_equal(raw2.info["dig"], raw.info["dig"])

    # the montage does not change
    assert_object_equal(montage.dig, test_montage.dig)

    # the montage should fulfill a roundtrip with make_dig_montage
    test2_montage = make_dig_montage(**montage.get_positions())
    assert_object_equal(test2_montage.dig, test_montage.dig)

    # 2. now do a standard montage
    montage = make_standard_montage("mgh60")
    # set the montage; note renaming to make standard montage map
    raw.set_montage(montage)

    # get montage back and set it
    # the channel locations should be the same
    raw2 = raw.copy()
    test_montage = raw.get_montage()
    raw.set_montage(test_montage, on_missing="ignore")

    # the montage should fulfill a roundtrip with make_dig_montage
    test2_montage = make_dig_montage(**test_montage.get_positions())
    assert_object_equal(test2_montage.dig, test_montage.dig)

    # chs should not change
    assert_object_equal(raw2.info["chs"], raw.info["chs"])
    # dig order might be different after set_montage
    assert montage.ch_names == test_montage.ch_names
    # note that test_montage will have different coordinate frame
    # compared to standard montage
    assert_dig_allclose(raw2.info, raw.info)
    assert_object_equal(raw2.info["dig"], raw.info["dig"])

    # 3. if montage gets set to None
    raw.set_montage(None)
    assert raw.get_montage() is None

    # 4. read in BV test dataset and make sure montage
    # fulfills roundtrip on non-standard montage
    dig_montage = read_dig_fif(fif_dig_montage_fname, verbose="error")

    # Make a BrainVision file like the one the user would have had
    # with testing dataset 'test.vhdr'
    raw_bv = read_raw_brainvision(bv_fname, preload=True)
    raw_bv_2 = raw_bv.copy()

    # rename channels to make it have the full set
    # of channels
    mapping = dict()
    for ii, ch_name in enumerate(raw_bv.ch_names, 1):
        mapping[ch_name] = f"EEG{ii:03d}"
    raw_bv.rename_channels(mapping)
    for ii, ch_name in enumerate(raw_bv_2.ch_names, 33):
        mapping[ch_name] = f"EEG{ii:03d}"
    raw_bv_2.rename_channels(mapping)
    raw_bv.add_channels([raw_bv_2])
    for ch in raw_bv.info["chs"]:
        ch["kind"] = FIFF.FIFFV_EEG_CH

    # Set the montage and roundtrip
    raw_bv.set_montage(dig_montage)
    raw_bv2 = raw_bv.copy()

    # reset the montage
    test_montage = raw_bv.get_montage()
    raw_bv.set_montage(test_montage, on_missing="ignore")
    # dig order might be different after set_montage
    assert_object_equal(raw_bv2.info["dig"], raw_bv.info["dig"])
    assert_dig_allclose(raw_bv2.info, raw_bv.info)

    # if dig is not set in the info, then montage returns None
    with raw.info._unlock():
        raw.info["dig"] = None
    assert raw.get_montage() is None

    # the montage should fulfill a roundtrip with make_dig_montage
    test2_montage = make_dig_montage(**test_montage.get_positions())
    assert_object_equal(test2_montage.dig, test_montage.dig)


def test_read_dig_hpts():
    """Test reading .hpts file (from MNE legacy)."""
    fname = io_dir / "brainvision" / "tests" / "data" / "test.hpts"
    montage = read_dig_hpts(fname)
    assert repr(montage) == (
        "<DigMontage | 0 extras (headshape), 5 HPIs, 3 fiducials, 34 channels>"
    )


def test_get_builtin_montages():
    """Test help function to obtain builtin montages."""
    EXPECTED_COUNT = 28

    montages = get_builtin_montages()
    assert len(montages) == EXPECTED_COUNT

    montages_with_descriptions = get_builtin_montages(descriptions=True)
    assert len(montages_with_descriptions) == EXPECTED_COUNT
    for montage_with_description in montages_with_descriptions:
        assert len(montage_with_description) == 2


@testing.requires_testing_data
def test_plot_montage():
    """Test plotting montage."""
    # gh-8025
    pytest.importorskip("defusedxml")
    montage = read_dig_captrak(bvct_dig_montage_fname)
    montage.plot()

    f, ax = plt.subplots(1, 1)
    montage.plot(axes=ax)

    with pytest.raises(TypeError, match="must be an instance of Axes"):
        montage.plot(axes=101)
    with pytest.raises(TypeError, match="when 'kind' is '3d'"):
        montage.plot(axes=ax, kind="3d")
    with pytest.raises(TypeError, match="when 'kind' is '3d'"):
        montage.plot(axes=101, kind="3d")


def test_montage_equality():
    """Test montage equality."""
    rng = np.random.RandomState(0)
    fiducials = dict(zip(("nasion", "lpa", "rpa"), rng.rand(3, 3)))

    # hsp positions are [1X, 1X, 1X]
    hsp1 = make_dig_montage(**fiducials, hsp=np.full((2, 3), 11.0))
    hsp2 = make_dig_montage(**fiducials, hsp=np.full((2, 3), 12.0))
    hsp2_identical = make_dig_montage(**fiducials, hsp=np.full((2, 3), 12.0))

    assert hsp1 != hsp2
    assert hsp2 == hsp2_identical


@testing.requires_testing_data
def test_montage_add_fiducials():
    """Test montage can add estimated fiducials for rpa, lpa, nas."""
    # get the fiducials from test file
    subjects_dir = data_path / "subjects"
    subject = "sample"
    fid_fname = subjects_dir / subject / "bem" / "sample-fiducials.fif"
    test_fids, _ = read_fiducials(fid_fname)
    test_fids = np.array([f["r"] for f in test_fids])

    # create test montage and add estimated fiducials
    test_ch_pos = {"A1": [0, 0, 0]}
    montage = make_dig_montage(ch_pos=test_ch_pos, coord_frame="mri")
    montage.add_estimated_fiducials(subject=subject, subjects_dir=subjects_dir)

    # check that adding MNI fiducials fails because we're in MRI
    with pytest.raises(
        RuntimeError, match='Montage should be in the "mni_tal" coordinate frame'
    ):
        montage.add_mni_fiducials(subjects_dir=subjects_dir)

    # check that these fiducials are close to the estimated fiducials
    ch_pos = montage.get_positions()
    fids_est = [ch_pos["lpa"], ch_pos["nasion"], ch_pos["rpa"]]

    dists = np.linalg.norm(test_fids - fids_est, axis=-1) * 1000.0  # -> mm
    assert (dists < 8).all(), dists

    # an error should be raised if the montage is not in `mri` coord_frame
    # which is the FreeSurfer RAS
    montage = make_dig_montage(ch_pos=test_ch_pos, coord_frame="mni_tal")
    with pytest.raises(
        RuntimeError, match='Montage should be in the "mri" coordinate frame'
    ):
        montage.add_estimated_fiducials(subject=subject, subjects_dir=subjects_dir)

    # test that adding MNI fiducials works
    montage.add_mni_fiducials(subjects_dir=subjects_dir)
    test_fids = get_mni_fiducials("fsaverage", subjects_dir=subjects_dir)
    for fid, test_fid in zip(montage.dig[:3], test_fids):
        assert_array_equal(fid["r"], test_fid["r"])

    # test remove fiducials
    montage.remove_fiducials()
    assert all([d["kind"] != FIFF.FIFFV_POINT_CARDINAL for d in montage.dig])


def test_read_dig_localite(tmp_path):
    """Test reading Localite .csv file."""
    contents = """#,id,x,y,z
                  1,Nasion,-2.016253511,6.243001715,34.63167712
                  2,LPA,71.96698724,-29.88835576,113.6703679
                  3,RPA,-82.77279316,-22.45928121,116.4005828
                  4,ch01,53.62814378,-91.37837488,29.69071863
                  5,ch02,54.02504821,-59.96228146,23.21714217
                  6,ch03,47.93261613,-29.99373786,24.56468867
                  7,ch04,29.04824633,-86.60006321,13.5073523
                  8,ch05,25.76285783,-58.1658606,3.854848377
                  9,ch06,25.39636794,-27.28186717,9.78490351
                  10,ch07,-5.181242819,-85.52115113,7.201882904
                  11,ch08,-4.995704801,-60.47053977,0.998486757
                  12,ch09,-2.680020493,-31.14357171,6.114621138
                  13,ch10,-33.65019131,-92.34198454,13.2326512
                  14,ch11,-36.22420417,-61.23822776,6.028649571
                  15,ch12,-33.21551039,-31.21772978,8.458854072
                  16,ch13,-61.38400606,-92.67546012,29.5783456
                  17,ch14,-61.16539571,-61.86866187,26.23986153
                  18,ch15,-55.82855386,-34.77319103,25.8083942"""

    fname = tmp_path / "localite.csv"
    with open(fname, "w") as f:
        for row in contents.split("\n"):
            f.write(f"{row.lstrip()}\n")
    montage = read_dig_localite(fname, nasion="Nasion", lpa="LPA", rpa="RPA")
    s = "<DigMontage | 0 extras (headshape), 0 HPIs, 3 fiducials, 15 channels>"
    assert repr(montage) == s
    assert montage.ch_names == [f"ch{i:02}" for i in range(1, 16)]


def test_make_wrong_dig_montage():
    """Test that a montage with non numeric is not possible."""
    make_dig_montage(ch_pos={"A1": ["0", "0", "0"]})  # converted to floats
    with pytest.raises(ValueError, match="could not convert string to float"):
        make_dig_montage(ch_pos={"A1": ["a", "b", "c"]})
    with pytest.raises(TypeError, match="instance of ndarray, list, or tuple"):
        make_dig_montage(ch_pos={"A1": 5})


@testing.requires_testing_data
def test_fnirs_montage():
    """Ensure fNIRS montages can be get and set."""
    raw = read_raw_nirx(fnirs_dname)
    info_orig = raw.copy().info
    mtg = raw.get_montage()

    num_sources = np.sum(["S" in optode for optode in mtg.ch_names])
    num_detectors = np.sum(["D" in optode for optode in mtg.ch_names])
    assert num_sources == 5
    assert num_detectors == 13

    # Make a change to the montage before setting
    raw.info["chs"][2]["loc"][:3] = [1.0, 2, 3]
    # Set montage back to original
    raw.set_montage(mtg)

    for ch in range(len(raw.ch_names)):
        assert_array_equal(info_orig["chs"][ch]["loc"], raw.info["chs"][ch]["loc"])

    # Mixed channel types not supported yet
    raw.set_channel_types({ch_name: "eeg" for ch_name in raw.ch_names[-2:]})
    with pytest.raises(ValueError, match="mix of fNIRS"):
        raw.get_montage()
