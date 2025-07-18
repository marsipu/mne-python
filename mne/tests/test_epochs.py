# Authors: The MNE-Python contributors.
# License: BSD-3-Clause
# Copyright the MNE-Python contributors.

import pickle
from copy import deepcopy
from datetime import timedelta
from functools import partial
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest
import scipy.signal
from numpy.fft import rfft, rfftfreq
from numpy.testing import (
    assert_allclose,
    assert_array_almost_equal,
    assert_array_equal,
    assert_array_less,
    assert_equal,
)

import mne
from mne import (
    Annotations,
    Epochs,
    combine_evoked,
    create_info,
    equalize_channels,
    make_fixed_length_epochs,
    make_fixed_length_events,
    pick_channels,
    pick_events,
    pick_types,
    read_epochs,
    read_events,
    read_evokeds,
    write_evokeds,
)
from mne._fiff.constants import FIFF
from mne._fiff.proj import _has_eeg_average_ref_proj
from mne._fiff.write import INT32_MAX, _get_split_size, write_float, write_int
from mne.annotations import _handle_meas_date
from mne.baseline import rescale
from mne.chpi import head_pos_to_trans_rot_t, read_head_pos
from mne.datasets import testing
from mne.epochs import (
    BaseEpochs,
    EpochsArray,
    _handle_event_repeated,
    average_movements,
    bootstrap,
    combine_event_ids,
    concatenate_epochs,
    equalize_epoch_counts,
    make_metadata,
)
from mne.event import merge_events
from mne.io import RawArray, read_raw_fif
from mne.preprocessing import maxwell_filter
from mne.utils import (
    _dt_to_stamp,
    _record_warnings,
    assert_meg_snr,
    catch_logging,
    object_diff,
    use_log_level,
)

data_path = testing.data_path(download=False)
fname_raw_testing = data_path / "MEG" / "sample" / "sample_audvis_trunc_raw.fif"
fname_raw_move = data_path / "SSS" / "test_move_anon_raw.fif"
fname_raw_movecomp_sss = data_path / "SSS" / "test_move_anon_movecomp_raw_sss.fif"
fname_raw_move_pos = data_path / "SSS" / "test_move_anon_raw.pos"

base_dir = Path(__file__).parents[1] / "io" / "tests" / "data"
raw_fname = base_dir / "test_raw.fif"
event_name = base_dir / "test-eve.fif"
evoked_nf_name = base_dir / "test-nf-ave.fif"

event_id, tmin, tmax = 1, -0.2, 0.5
event_id_2 = np.int64(2)  # to test non Python int types
rng = np.random.RandomState(42)


def _create_epochs_with_annotations():
    """Create test dataset of Epochs with Annotations."""
    # set up a test dataset
    data = rng.randn(1, 600)
    sfreq = 100.0
    info = create_info(ch_names=["MEG1"], ch_types=["grad"], sfreq=sfreq)
    raw = RawArray(data, info)

    # epoch onsets will be at 0.5, 2.5, 4.5s and will be one second long
    events = np.zeros((3, 3), dtype=int)
    events[:, 0] = (np.array([0.5, 2.5, 4.5]) * sfreq).astype(int)

    # make annotations to test various kinds of overlap
    #         onset  dur  descr
    annots = [
        (0.3, 0.0, "no_overlap"),
        (0.4, 0.1, "coincident_onset"),  # only edge coincides
        (0.4, 0.2, "straddles_onset"),
        (1.4, 0.2, "straddles_offset"),
        (1.5, 0.0, "coincident_offset"),  # only edge coincides, zero-dur
        (2.6, 0.0, "within_epoch"),
        (4.4, 1.2, "surround_epoch"),
        (3.4, 1.2, "multiple"),
    ]
    annots = Annotations(*zip(*annots))
    raw.set_annotations(annots)
    epochs = Epochs(raw, events=events, tmin=0, tmax=1, baseline=None)
    return epochs, raw, events


def test_event_repeated():
    """Test epochs takes into account repeated events."""
    n_samples = 100
    n_channels = 2
    ch_names = [f"chan{i}" for i in range(n_channels)]
    info = mne.create_info(ch_names=ch_names, sfreq=1000.0)
    data = np.zeros((n_channels, n_samples))
    raw = mne.io.RawArray(data, info)

    events = np.array([[10, 0, 1], [10, 0, 2]])
    epochs = mne.Epochs(raw, events, event_repeated="drop")
    assert epochs.drop_log == ((), ("DROP DUPLICATE",))
    assert_array_equal(epochs.selection, [0])
    epochs = mne.Epochs(raw, events, event_repeated="merge")
    assert epochs.drop_log == ((), ("MERGE DUPLICATE",))
    assert_array_equal(epochs.selection, [0])


def test_handle_event_repeated():
    """Test handling of repeated events."""
    # A general test case
    EVENT_ID = {"aud": 1, "vis": 2, "foo": 3}
    EVENTS = np.array(
        [
            [0, 0, 1],
            [0, 0, 2],
            [3, 0, 2],
            [3, 0, 1],
            [5, 0, 2],
            [5, 0, 1],
            [5, 0, 3],
            [7, 0, 1],
        ]
    )
    SELECTION = np.arange(len(EVENTS))
    DROP_LOG = ((),) * len(EVENTS)
    with pytest.raises(RuntimeError, match="Event time samples were not uniq"):
        _handle_event_repeated(
            EVENTS,
            EVENT_ID,
            event_repeated="error",
            selection=SELECTION,
            drop_log=DROP_LOG,
        )

    events, event_id, selection, drop_log = _handle_event_repeated(
        EVENTS, EVENT_ID, "drop", SELECTION, DROP_LOG
    )
    assert_array_equal(events, [[0, 0, 1], [3, 0, 2], [5, 0, 2], [7, 0, 1]])
    assert_array_equal(events, EVENTS[selection])
    unselection = np.setdiff1d(SELECTION, selection)
    assert all(drop_log[k] == ("DROP DUPLICATE",) for k in unselection)
    assert event_id == {"aud": 1, "vis": 2}

    events, event_id, selection, drop_log = _handle_event_repeated(
        EVENTS, EVENT_ID, "merge", SELECTION, DROP_LOG
    )
    assert_array_equal(events[0][-1], events[1][-1])
    assert_array_equal(events, [[0, 0, 4], [3, 0, 4], [5, 0, 5], [7, 0, 1]])
    assert_array_equal(events[:, :2], EVENTS[selection][:, :2])
    unselection = np.setdiff1d(SELECTION, selection)
    assert all(drop_log[k] == ("MERGE DUPLICATE",) for k in unselection)
    assert set(event_id.keys()) == set(["aud", "aud/vis", "aud/foo/vis"])
    assert event_id["aud/vis"] == 4

    # Test early return with no changes: no error for wrong event_repeated arg
    fine_events = np.array([[0, 0, 1], [1, 0, 2]])
    events, event_id, selection, drop_log = _handle_event_repeated(
        fine_events, EVENT_ID, "no", [0, 2], DROP_LOG
    )
    assert event_id == EVENT_ID
    assert_array_equal(selection, [0, 2])
    assert drop_log == DROP_LOG
    assert_array_equal(events, fine_events)
    del fine_events

    # Test falling back on 0 for heterogeneous "prior-to-event" codes
    # order of third column does not determine new event_id key, we always
    # take components, sort, and join on "/"
    # should make new event_id value: 5 (because 1,2,3,4 are taken)
    heterogeneous_events = np.array([[0, 3, 2], [0, 4, 1]])
    events, event_id, selection, drop_log = _handle_event_repeated(
        heterogeneous_events, EVENT_ID, "merge", [0, 1], deepcopy(DROP_LOG)
    )
    assert set(event_id.keys()) == set(["aud/vis"])
    assert event_id["aud/vis"] == 5
    assert_array_equal(selection, [0])
    assert drop_log[1] == ("MERGE DUPLICATE",)
    assert_array_equal(
        events,
        np.array(
            [
                [0, 0, 5],
            ]
        ),
    )
    del heterogeneous_events

    # Test keeping a homogeneous "prior-to-event" code (=events[:, 1])
    homogeneous_events = np.array([[0, 99, 1], [0, 99, 2], [1, 0, 1], [2, 0, 2]])
    events, event_id, selection, drop_log = _handle_event_repeated(
        homogeneous_events, EVENT_ID, "merge", [1, 3, 4, 7], deepcopy(DROP_LOG)
    )
    assert set(event_id.keys()) == set(["aud", "vis", "aud/vis"])
    assert_array_equal(events, np.array([[0, 99, 4], [1, 0, 1], [2, 0, 2]]))
    assert_array_equal(selection, [1, 4, 7])
    assert drop_log[3] == ("MERGE DUPLICATE",)
    del homogeneous_events

    # Test dropping instead of merging, if event_codes to be merged are equal
    equal_events = np.array([[0, 0, 1], [0, 0, 1]])
    events, event_id, selection, drop_log = _handle_event_repeated(
        equal_events, EVENT_ID, "merge", [3, 5], deepcopy(DROP_LOG)
    )
    assert_array_equal(
        events,
        np.array(
            [
                [0, 0, 1],
            ]
        ),
    )
    assert_array_equal(selection, [3])
    assert drop_log[5] == ("MERGE DUPLICATE",)
    assert set(event_id.keys()) == set(["aud"])

    # new numbers
    for vals, want in (((1, 3), 2), ((2, 3), 1), ((1, 2), 3)):
        events = np.zeros((2, 3), int)
        events[:, 2] = vals
        event_id = {str(v): v for v in events[:, 2]}
        selection = np.arange(len(events))
        drop_log = [tuple() for _ in range(len(events))]
        events, event_id, selection, drop_log = _handle_event_repeated(
            events, event_id, "merge", selection, drop_log
        )
        want = np.array([[0, 0, want]])
        assert_array_equal(events, want)


def _get_data(preload=False):
    """Get data."""
    raw = read_raw_fif(raw_fname, preload=preload, verbose="warning")
    events = read_events(event_name)
    picks = pick_types(
        raw.info,
        meg=True,
        eeg=True,
        stim=True,
        ecg=True,
        eog=True,
        include=["STI 014"],
        exclude="bads",
    )
    return raw, events, picks


reject = dict(grad=1000e-12, mag=4e-12, eeg=80e-6, eog=150e-6)
flat = dict(grad=1e-15, mag=1e-15)


def test_get_data_copy():
    """Test the .get_data() method."""
    raw, events, picks = _get_data()
    event_id = {"a/1": 1, "a/2": 2, "b/1": 3, "b/2": 4}
    epochs = Epochs(raw, events, event_id, preload=True)

    # Testing with respect to units param
    # more tests in mne/io/tests/test_raw.py::test_get_data_units
    # EEG is already in V, so no conversion should take place
    d1 = epochs.get_data(picks="eeg", units=None)
    d2 = epochs.get_data(picks="eeg", units="V")
    assert_array_equal(d1, d2)

    with pytest.raises(ValueError, match="is not a valid unit for eeg"):
        epochs.get_data(picks="eeg", units="")

    with pytest.raises(ValueError, match="cannot be str if there is more"):
        epochs.get_data(picks=["eeg", "meg"], units="V")

    # Check combination of units with item param, scale only one ch_type
    d3 = epochs.get_data(item=[1, 2, 3], units={"grad": "fT/cm"})
    assert d3.shape[0] == 3

    grad_idxs = np.array([i == "grad" for i in epochs.get_channel_types()])
    eeg_idxs = np.array([i == "eeg" for i in epochs.get_channel_types()])
    assert_array_equal(
        d3[:, grad_idxs, :],
        epochs.get_data("grad", item=[1, 2, 3]) * 1e13,  # T/m to fT/cm
    )
    assert_array_equal(d3[:, eeg_idxs, :], epochs.get_data("eeg", item=[1, 2, 3]))

    # Test tmin/tmax
    data = epochs.get_data(tmin=0)
    assert np.all(
        data.shape[-1] == epochs._data.shape[-1] - np.nonzero(epochs.times == 0)[0]
    )

    assert epochs.get_data(tmin=0, tmax=0).size == 0

    with pytest.raises(TypeError, match="tmin .* float, None"):
        epochs.get_data(tmin=[1], tmax=1)

    with pytest.raises(TypeError, match="tmax .* float, None"):
        epochs.get_data(tmin=1, tmax=np.ones(5))

    # Test copy
    data = epochs.get_data(copy=True)
    assert not np.shares_memory(data, epochs._data)

    data = epochs.get_data(copy=False, verbose="debug")
    assert np.shares_memory(data, epochs._data)
    assert data is epochs._data
    data_orig = data.copy()
    # picks, item, and units must be None
    data = epochs.get_data(copy=False, picks=[1])
    assert not np.shares_memory(data, epochs._data)
    data = epochs.get_data(copy=False, item=[0])
    assert not np.shares_memory(data, epochs._data)
    data = epochs.get_data(copy=False, units=dict(eeg="uV"))
    assert not np.shares_memory(data, epochs._data)
    # Make sure we didn't mess up our values
    assert_allclose(data_orig, epochs._data)


def test_hierarchical():
    """Test hierarchical access."""
    raw, events, picks = _get_data()
    event_id = {"a/1": 1, "a/2": 2, "b/1": 3, "b/2": 4}
    epochs = Epochs(raw, events, event_id, preload=True)
    epochs_a1 = epochs["a/1"]
    epochs_a2 = epochs["a/2"]
    epochs_b1 = epochs["b/1"]
    epochs_b2 = epochs["b/2"]
    epochs_a = epochs["a"]
    assert_equal(len(epochs_a), len(epochs_a1) + len(epochs_a2))
    epochs_b = epochs["b"]
    assert_equal(len(epochs_b), len(epochs_b1) + len(epochs_b2))
    epochs_1 = epochs["1"]
    assert_equal(len(epochs_1), len(epochs_a1) + len(epochs_b1))
    epochs_2 = epochs["2"]
    assert_equal(len(epochs_2), len(epochs_a2) + len(epochs_b2))
    epochs_all = epochs[("1", "2")]
    assert_equal(len(epochs), len(epochs_all))
    assert_array_equal(epochs.get_data(), epochs_all.get_data())


@pytest.mark.slowtest
@testing.requires_testing_data
def test_average_movements():
    """Test movement averaging algorithm."""
    # usable data
    crop = 0.0, 10.0
    origin = (0.0, 0.0, 0.04)
    raw = read_raw_fif(fname_raw_move, allow_maxshield="yes")
    raw.info["bads"] += ["MEG2443"]  # mark some bad MEG channel
    raw.crop(*crop).load_data()
    raw.filter(None, 20, fir_design="firwin")
    events = make_fixed_length_events(raw, event_id)
    picks = pick_types(
        raw.info, meg=True, eeg=True, stim=True, ecg=True, eog=True, exclude=()
    )
    epochs = Epochs(
        raw, events, event_id, tmin, tmax, picks=picks, proj=False, preload=True
    )
    with pytest.warns(RuntimeWarning, match="were dropped"):
        epochs_proj = Epochs(
            raw, events[:1], event_id, tmin, tmax, picks=picks, proj=True, preload=True
        )
    raw_sss_stat = maxwell_filter(
        raw, origin=origin, regularize=None, bad_condition="ignore"
    )
    del raw
    epochs_sss_stat = Epochs(
        raw_sss_stat, events, event_id, tmin, tmax, picks=picks, proj=False
    )
    evoked_sss_stat = epochs_sss_stat.average()
    del raw_sss_stat, epochs_sss_stat
    head_pos = read_head_pos(fname_raw_move_pos)
    trans = epochs.info["dev_head_t"]["trans"]
    head_pos_stat = (
        np.array([trans[:3, 3]]),
        np.array([trans[:3, :3]]),
        np.array([0.0]),
    )

    # SSS-based
    pytest.raises(TypeError, average_movements, epochs, None)
    evoked_move_non = average_movements(
        epochs, head_pos=head_pos, weight_all=False, origin=origin
    )
    evoked_move_all = average_movements(
        epochs, head_pos=head_pos, weight_all=True, origin=origin
    )
    evoked_stat_all = average_movements(
        epochs, head_pos=head_pos_stat, weight_all=True, origin=origin
    )
    evoked_std = epochs.average()
    for ev in (evoked_move_non, evoked_move_all, evoked_stat_all):
        assert_equal(ev.nave, evoked_std.nave)
        assert_equal(len(ev.info["bads"]), 0)
    # substantial changes to MEG data
    for ev in (evoked_move_non, evoked_stat_all):
        assert_meg_snr(ev, evoked_std, 0.0, 0.1)
        pytest.raises(AssertionError, assert_meg_snr, ev, evoked_std, 1.0, 1.0)
    meg_picks = pick_types(evoked_std.info, meg=True, exclude=())
    assert_allclose(
        evoked_move_non.data[meg_picks], evoked_move_all.data[meg_picks], atol=1e-20
    )
    # compare to averaged movecomp version (should be fairly similar)
    raw_sss = read_raw_fif(fname_raw_movecomp_sss)
    raw_sss.crop(*crop).load_data()
    raw_sss.filter(None, 20, fir_design="firwin")
    picks_sss = pick_types(
        raw_sss.info, meg=True, eeg=True, stim=True, ecg=True, eog=True, exclude=()
    )
    assert_array_equal(picks, picks_sss)
    epochs_sss = Epochs(
        raw_sss, events, event_id, tmin, tmax, picks=picks_sss, proj=False
    )
    evoked_sss = epochs_sss.average()
    assert_equal(evoked_std.nave, evoked_sss.nave)
    # this should break the non-MEG channels
    pytest.raises(AssertionError, assert_meg_snr, evoked_sss, evoked_move_all, 0.0, 0.0)
    assert_meg_snr(evoked_sss, evoked_move_non, 0.02, 2.6)
    assert_meg_snr(evoked_sss, evoked_stat_all, 0.05, 3.2)
    # these should be close to numerical precision
    assert_allclose(evoked_sss_stat.data, evoked_stat_all.data, atol=1e-14)

    # pos[0] > epochs.events[0] uses dev_head_t, so make it equivalent
    destination = deepcopy(epochs.info["dev_head_t"])
    x = head_pos_to_trans_rot_t(head_pos[1])
    epochs.info["dev_head_t"]["trans"][:3, :3] = x[1]
    epochs.info["dev_head_t"]["trans"][:3, 3] = x[0]
    pytest.raises(
        AssertionError,
        assert_allclose,
        epochs.info["dev_head_t"]["trans"],
        destination["trans"],
    )
    evoked_miss = average_movements(
        epochs, head_pos=head_pos[2:], origin=origin, destination=destination
    )
    assert_allclose(evoked_miss.data, evoked_move_all.data, atol=1e-20)
    assert_allclose(evoked_miss.info["dev_head_t"]["trans"], destination["trans"])

    # degenerate cases
    destination["to"] = destination["from"]  # bad dest
    pytest.raises(
        RuntimeError,
        average_movements,
        epochs,
        head_pos,
        origin=origin,
        destination=destination,
    )
    pytest.raises(TypeError, average_movements, "foo", head_pos=head_pos)
    pytest.raises(
        RuntimeError, average_movements, epochs_proj, head_pos=head_pos
    )  # prj


def _assert_drop_log_types(drop_log):
    __tracebackhide__ = True
    assert isinstance(drop_log, tuple), "drop_log should be tuple"
    assert all(isinstance(log, tuple) for log in drop_log), (
        "drop_log[ii] should be tuple"
    )
    assert all(isinstance(s, str) for log in drop_log for s in log), (
        "drop_log[ii][jj] should be str"
    )


def test_reject():
    """Test epochs rejection."""
    raw, events, _ = _get_data()
    names = raw.ch_names[::5]
    assert "MEG 2443" in names
    raw.pick(names).load_data()
    assert "eog" in raw
    raw.info.normalize_proj()
    picks = np.arange(len(raw.ch_names))
    # cull the list just to contain the relevant event
    events = events[events[:, 2] == event_id, :]
    assert len(events) == 7
    selection = np.arange(3)
    drop_log = ((),) * 3 + (("MEG 2443",),) * 4
    _assert_drop_log_types(drop_log)
    pytest.raises(TypeError, pick_types, raw)
    picks_meg = pick_types(raw.info, meg=True, eeg=False)
    pytest.raises(
        TypeError,
        Epochs,
        raw,
        events,
        event_id,
        tmin,
        tmax,
        picks=picks,
        preload=False,
        reject="foo",
    )
    pytest.raises(
        ValueError,
        Epochs,
        raw,
        events,
        event_id,
        tmin,
        tmax,
        picks=picks_meg,
        preload=False,
        reject=dict(eeg=1.0),
    )
    # this one is okay because it's not actually requesting rejection
    Epochs(
        raw,
        events,
        event_id,
        tmin,
        tmax,
        picks=picks_meg,
        preload=False,
        reject=dict(eeg=np.inf),
    )

    # Good function
    def my_reject_1(epoch_data):
        bad_idxs = np.where(np.percentile(epoch_data, 90, axis=1) > 1e-35)
        reasons = "a" * len(bad_idxs[0])
        return len(bad_idxs) > 0, reasons

    # Bad function
    def my_reject_2(epoch_data):
        bad_idxs = np.where(np.percentile(epoch_data, 90, axis=1) > 1e-35)
        reasons = "a" * len(bad_idxs[0])
        return len(bad_idxs), reasons

    for val in (-1, -2):  # protect against older MNE-C types
        for kwarg in ("reject", "flat"):
            pytest.raises(
                ValueError,
                Epochs,
                raw,
                events,
                event_id,
                tmin,
                tmax,
                picks=picks_meg,
                preload=False,
                **{kwarg: dict(grad=val)},
            )

    # Check that reject and flat in constructor are not callables
    val = my_reject_1
    for kwarg in ("reject", "flat"):
        with pytest.raises(
            TypeError,
            match=r".* must be an instance of numeric, got <class 'function'> instead.",
        ):
            Epochs(
                raw,
                events,
                event_id,
                tmin,
                tmax,
                picks=picks_meg,
                preload=False,
                **{kwarg: dict(grad=val)},
            )

    # Check if callable returns a tuple with reasons
    bad_types = [my_reject_2, ("HiHi"), (1, 1), None]
    for val in bad_types:  # protect against bad types
        for kwarg in ("reject", "flat"):
            with pytest.raises(
                TypeError,
                match=r".* must be an instance of .* got .* instead.",
            ):
                epochs = Epochs(
                    raw,
                    events,
                    event_id,
                    tmin,
                    tmax,
                    picks=picks_meg,
                    preload=True,
                )
                epochs.drop_bad(**{kwarg: dict(grad=val)})

    pytest.raises(
        KeyError,
        Epochs,
        raw,
        events,
        event_id,
        tmin,
        tmax,
        picks=picks,
        preload=False,
        reject=dict(foo=1.0),
    )

    data_7 = dict()
    keep_idx = [0, 1, 2]
    for preload in (True, False):
        for proj in (True, False, "delayed"):
            # no rejection
            epochs = Epochs(
                raw, events, event_id, tmin, tmax, picks=picks, preload=preload
            )
            _assert_drop_log_types(epochs.drop_log)
            pytest.raises(ValueError, epochs.drop_bad, reject="foo")
            epochs.drop_bad()
            assert_equal(len(epochs), len(events))
            assert_array_equal(epochs.selection, np.arange(len(events)))
            assert epochs.drop_log == ((),) * 7
            if proj not in data_7:
                data_7[proj] = epochs.get_data()
            assert_array_equal(epochs.get_data(), data_7[proj])

            # with rejection
            epochs = Epochs(
                raw,
                events,
                event_id,
                tmin,
                tmax,
                picks=picks,
                reject=reject,
                preload=preload,
            )
            _assert_drop_log_types(epochs.drop_log)
            epochs.drop_bad()
            _assert_drop_log_types(epochs.drop_log)
            assert_equal(len(epochs), len(events) - 4)
            assert_array_equal(epochs.selection, selection)
            assert epochs.drop_log == drop_log
            assert_array_equal(epochs.get_data(), data_7[proj][keep_idx])

            # rejection post-hoc
            epochs = Epochs(
                raw, events, event_id, tmin, tmax, picks=picks, preload=preload
            )
            epochs.drop_bad()
            assert_equal(len(epochs), len(events))
            assert_array_equal(epochs.get_data(), data_7[proj])
            epochs.drop_bad(reject)
            assert_equal(len(epochs), len(events) - 4)
            assert_equal(len(epochs), len(epochs.get_data()))
            assert_array_equal(epochs.selection, selection)
            assert epochs.drop_log == drop_log
            assert_array_equal(epochs.get_data(), data_7[proj][keep_idx])

            # rejection twice
            reject_part = dict(grad=1100e-12, mag=4e-12, eeg=80e-6, eog=150e-6)
            epochs = Epochs(
                raw,
                events,
                event_id,
                tmin,
                tmax,
                picks=picks,
                reject=reject_part,
                preload=preload,
            )
            epochs.drop_bad()
            assert_equal(len(epochs), len(events) - 1)
            epochs.drop_bad(reject)
            assert_equal(len(epochs), len(events) - 4)
            assert_array_equal(epochs.selection, selection)
            assert epochs.drop_log == drop_log
            assert_array_equal(epochs.get_data(), data_7[proj][keep_idx])

            # ensure that thresholds must become more stringent, not less
            pytest.raises(ValueError, epochs.drop_bad, reject_part)
            assert_equal(len(epochs), len(events) - 4)
            assert_array_equal(epochs.get_data(), data_7[proj][keep_idx])
            with pytest.warns(RuntimeWarning, match="were dropped"):
                epochs.drop_bad(flat=dict(mag=1.0))
            assert_equal(len(epochs), 0)
            pytest.raises(ValueError, epochs.drop_bad, flat=dict(mag=0.0))

            # rejection of subset of trials (ensure array ownership)
            reject_part = dict(grad=1100e-12, mag=4e-12, eeg=80e-6, eog=150e-6)
            epochs = Epochs(
                raw,
                events,
                event_id,
                tmin,
                tmax,
                picks=picks,
                reject=None,
                preload=preload,
            )
            epochs = epochs[:-1]
            epochs.drop_bad(reject=reject)
            assert_equal(len(epochs), len(events) - 4)
            assert_array_equal(epochs.get_data(), data_7[proj][keep_idx])

        # rejection on annotations
        sfreq = raw.info["sfreq"]
        onsets = [(event[0] - raw.first_samp) / sfreq for event in events[::2][:3]]
        onsets[0] = onsets[0] + tmin - 0.499  # tmin < 0
        onsets[1] = onsets[1] + tmax - 0.001
        stamp = _dt_to_stamp(raw.info["meas_date"])
        first_time = stamp[0] + stamp[1] * 1e-6 + raw.first_samp / sfreq
        for orig_time in [None, first_time]:
            annot = Annotations(onsets, [0.5, 0.5, 0.5], "BAD", orig_time)
            raw.set_annotations(annot)
            epochs = Epochs(
                raw,
                events,
                event_id,
                tmin,
                tmax,
                picks=[0],
                reject=None,
                preload=preload,
            )
            epochs.drop_bad()
            assert_equal(len(events) - 3, len(epochs.events))
            assert_equal(epochs.drop_log[0][0], "BAD")
            assert_equal(epochs.drop_log[2][0], "BAD")
            assert_equal(epochs.drop_log[4][0], "BAD")
        raw.set_annotations(None)

        # rejection with all None / False arguments: no loading / dropping
        epochs = Epochs(
            raw,
            events,
            event_id,
            tmin,
            tmax,
            picks=[0],
            reject=None,
            flat=None,
            reject_by_annotation=False,
            reject_tmin=None,
            reject_tmax=None,
        )
        with catch_logging() as log:
            epochs.drop_bad(verbose="debug")
        log = log.getvalue()
        assert "is a noop" in log


def test_reject_by_annotations_reject_tmin_reject_tmax():
    """Test reject_by_annotations with reject_tmin and reject_tmax defined."""
    # 10 seconds of data, event at 2s, bad segment from 1s to 1.5s
    info = mne.create_info(ch_names=["test_a"], sfreq=1000, ch_types="eeg")
    raw = mne.io.RawArray(np.atleast_2d(np.arange(0, 10, 1 / 1000)), info=info)
    events = np.array([[2000, 0, 1]])
    raw.set_annotations(mne.Annotations(1, 0.5, "BAD"))

    # Make the epoch based on the event at 2s, so from 1s to 3s ... assert it
    # is rejected due to bad segment overlap from 1s to 1.5s
    with pytest.warns(RuntimeWarning, match="were dropped"):
        epochs = mne.Epochs(
            raw, events, tmin=-1, tmax=1, preload=True, reject_by_annotation=True
        )
    assert len(epochs) == 0

    # Setting `reject_tmin` to prevent rejection of epoch.
    epochs = mne.Epochs(
        raw,
        events,
        tmin=-1,
        tmax=1,
        reject_tmin=-0.2,
        preload=True,
        reject_by_annotation=True,
    )
    assert len(epochs) == 1

    # Same check but bad segment overlapping from 2.5s to 3s: use `reject_tmax`
    raw.set_annotations(mne.Annotations(2.5, 0.5, "BAD"))
    epochs = mne.Epochs(
        raw,
        events,
        tmin=-1,
        tmax=1,
        reject_tmax=0.4,
        preload=True,
        reject_by_annotation=True,
    )
    assert len(epochs) == 1


def test_own_data():
    """Test for epochs data ownership (gh-5346)."""
    raw, events = _get_data()[:2]
    n_epochs = 10
    events = events[:n_epochs]
    epochs = mne.Epochs(raw, events, preload=True)
    assert epochs._data.flags["C_CONTIGUOUS"]
    assert epochs._data.flags["OWNDATA"]
    epochs.crop(tmin=-0.1, tmax=0.4)
    assert len(epochs) == epochs._data.shape[0] == len(epochs.events)
    assert len(epochs) == n_epochs
    assert not epochs._data.flags["OWNDATA"]

    # data ownership value error
    epochs.drop_bad(flat=dict(eeg=8e-6))
    n_now = len(epochs)
    assert 5 < n_now < n_epochs
    assert len(epochs) == epochs._data.shape[0] == len(epochs.events)

    good_chan = epochs.copy().pick([epochs.ch_names[0]])
    good_chan.rename_channels({good_chan.ch_names[0]: "good"})
    epochs.add_channels([good_chan])
    # "ValueError: resize only works on single-segment arrays"
    epochs.drop_bad(flat=dict(eeg=10e-6))
    assert 1 < len(epochs) < n_now


def test_decim():
    """Test epochs decimation."""
    # First with EpochsArray
    dec_1, dec_2 = 2, 3
    decim = dec_1 * dec_2
    n_epochs, n_channels, n_times = 5, 10, 20
    sfreq = 1000.0
    sfreq_new = sfreq / decim
    data = rng.randn(n_epochs, n_channels, n_times)
    events = np.array([np.arange(n_epochs), [0] * n_epochs, [1] * n_epochs]).T
    info = create_info(n_channels, sfreq, "eeg")
    with info._unlock():
        info["lowpass"] = sfreq_new / float(decim)
    epochs = EpochsArray(data, info, events)
    data_epochs = epochs.copy().decimate(decim).get_data()
    data_epochs_2 = epochs.copy().decimate(decim, offset=1).get_data()
    data_epochs_3 = epochs.decimate(dec_1).decimate(dec_2).get_data()
    assert_array_equal(data_epochs, data[:, :, ::decim])
    assert_array_equal(data_epochs_2, data[:, :, 1::decim])
    assert_array_equal(data_epochs, data_epochs_3)

    # Now let's do it with some real data
    raw, events, picks = _get_data()
    events = events[events[:, 2] == 1][:2]
    raw.load_data().pick([raw.ch_names[pick] for pick in picks[::30]])
    raw.info.normalize_proj()
    del picks
    sfreq_new = raw.info["sfreq"] / decim
    with raw.info._unlock():
        raw.info["lowpass"] = sfreq_new / 12.0  # suppress aliasing warnings
    pytest.raises(ValueError, epochs.decimate, -1)
    pytest.raises(ValueError, epochs.decimate, 2, offset=-1)
    pytest.raises(ValueError, epochs.decimate, 2, offset=2)
    for this_offset in range(decim):
        epochs = Epochs(
            raw,
            events,
            event_id,
            tmin=-this_offset / raw.info["sfreq"],
            tmax=tmax,
            baseline=None,
        )
        idx_offsets = np.arange(decim) + this_offset
        for offset, idx_offset in zip(np.arange(decim), idx_offsets):
            expected_times = epochs.times[idx_offset::decim]
            expected_data = epochs.get_data()[:, :, idx_offset::decim]
            must_have = offset / float(epochs.info["sfreq"])
            assert np.isclose(must_have, expected_times).any()
            ep_decim = epochs.copy().decimate(decim, offset)
            assert np.isclose(must_have, ep_decim.times).any()
            assert_allclose(ep_decim.times, expected_times)
            assert_allclose(ep_decim.get_data(), expected_data)
            assert_equal(ep_decim.info["sfreq"], sfreq_new)

    # More complicated cases
    epochs = Epochs(raw, events, event_id, tmin, tmax)
    expected_data = epochs.get_data()[:, :, ::decim]
    expected_times = epochs.times[::decim]
    for preload in (True, False):
        # at init
        epochs = Epochs(raw, events, event_id, tmin, tmax, decim=decim, preload=preload)
        assert_allclose(epochs.get_data(), expected_data)
        assert_allclose(epochs.get_data(), expected_data)
        assert_equal(epochs.info["sfreq"], sfreq_new)
        assert_array_equal(epochs.times, expected_times)

        # split between init and afterward
        epochs = Epochs(
            raw, events, event_id, tmin, tmax, decim=dec_1, preload=preload
        ).decimate(dec_2)
        assert_allclose(epochs.get_data(), expected_data)
        assert_allclose(epochs.get_data(), expected_data)
        assert_equal(epochs.info["sfreq"], sfreq_new)
        assert_array_equal(epochs.times, expected_times)
        epochs = Epochs(
            raw, events, event_id, tmin, tmax, decim=dec_2, preload=preload
        ).decimate(dec_1)
        assert_allclose(epochs.get_data(), expected_data)
        assert_allclose(epochs.get_data(), expected_data)
        assert_equal(epochs.info["sfreq"], sfreq_new)
        assert_array_equal(epochs.times, expected_times)

        # split between init and afterward, with preload in between
        epochs = Epochs(raw, events, event_id, tmin, tmax, decim=dec_1, preload=preload)
        epochs.load_data()
        epochs = epochs.decimate(dec_2)
        assert_allclose(epochs.get_data(), expected_data)
        assert_allclose(epochs.get_data(), expected_data)
        assert_equal(epochs.info["sfreq"], sfreq_new)
        assert_array_equal(epochs.times, expected_times)
        epochs = Epochs(raw, events, event_id, tmin, tmax, decim=dec_2, preload=preload)
        epochs.load_data()
        epochs = epochs.decimate(dec_1)
        assert_allclose(epochs.get_data(), expected_data)
        assert_allclose(epochs.get_data(), expected_data)
        assert_equal(epochs.info["sfreq"], sfreq_new)
        assert_array_equal(epochs.times, expected_times)

        # decimate afterward
        epochs = Epochs(raw, events, event_id, tmin, tmax, preload=preload).decimate(
            decim
        )
        assert_allclose(epochs.get_data(), expected_data)
        assert_allclose(epochs.get_data(), expected_data)
        assert_equal(epochs.info["sfreq"], sfreq_new)
        assert_array_equal(epochs.times, expected_times)

        # decimate afterward, with preload in between
        epochs = Epochs(raw, events, event_id, tmin, tmax, preload=preload)
        epochs.load_data()
        epochs.decimate(decim)
        assert_allclose(epochs.get_data(), expected_data)
        assert_allclose(epochs.get_data(), expected_data)
        assert_equal(epochs.info["sfreq"], sfreq_new)
        assert_array_equal(epochs.times, expected_times)

        # test picks when getting data
        picks = [3, 4, 7]
        d1 = epochs.get_data(picks=picks)
        d2 = epochs.get_data()[:, picks]
        assert_array_equal(d1, d2)


def test_base_epochs():
    """Test base epochs class."""
    raw = _get_data()[0]
    epochs = BaseEpochs(raw.info, None, np.ones((1, 3), int), event_id, tmin, tmax)
    pytest.raises(NotImplementedError, epochs.get_data)
    # events have wrong dtype (float)
    with pytest.raises(TypeError, match="events should be a NumPy array"):
        BaseEpochs(raw.info, None, np.ones((1, 3), float), event_id, tmin, tmax)
    # events have wrong shape
    with pytest.raises(ValueError, match="events must be of shape"):
        BaseEpochs(raw.info, None, np.ones((1, 3, 2), int), event_id, tmin, tmax)
    # events are tuple (like returned by mne.events_from_annotations)
    with pytest.raises(TypeError, match="events should be a NumPy array"):
        BaseEpochs(raw.info, None, (np.ones((1, 3), int), {"foo": 1}))


def test_savgol_filter():
    """Test savgol filtering."""
    h_freq = 20.0
    raw, events = _get_data()[:2]
    epochs = Epochs(raw, events, event_id, tmin, tmax)
    pytest.raises(RuntimeError, epochs.savgol_filter, 10.0)
    epochs = Epochs(raw, events, event_id, tmin, tmax, preload=True)
    epochs.pick(picks="grad")
    freqs = rfftfreq(len(epochs.times), 1.0 / epochs.info["sfreq"])
    data = np.abs(rfft(epochs.get_data()))
    pass_mask = freqs <= h_freq / 2.0 - 5.0
    stop_mask = freqs >= h_freq * 2 + 5.0
    epochs.savgol_filter(h_freq)
    data_filt = np.abs(rfft(epochs.get_data()))
    # decent in pass-band
    assert_allclose(
        np.mean(data[:, :, pass_mask], 0),
        np.mean(data_filt[:, :, pass_mask], 0),
        rtol=1e-2,
        atol=1e-18,
    )
    # suppression in stop-band
    assert np.mean(data[:, :, stop_mask]) > np.mean(data_filt[:, :, stop_mask]) * 5


def test_filter(tmp_path):
    """Test filtering."""
    h_freq = 40.0
    raw, events = _get_data()[:2]
    epochs = Epochs(raw, events, event_id, tmin, tmax)
    assert round(epochs.info["lowpass"]) == 172
    pytest.raises(RuntimeError, epochs.savgol_filter, 10.0)
    epochs = Epochs(raw, events, event_id, tmin, tmax, preload=True)
    epochs.pick(picks="grad")
    freqs = rfftfreq(len(epochs.times), 1.0 / epochs.info["sfreq"])
    data_fft = np.abs(rfft(epochs.get_data()))
    pass_mask = freqs <= h_freq / 2.0 - 5.0
    stop_mask = freqs >= h_freq * 2 + 5.0
    epochs_orig = epochs.copy()
    epochs.filter(None, h_freq)
    assert epochs.info["lowpass"] == h_freq
    data_filt = epochs.get_data()
    data_filt_fft = np.abs(rfft(data_filt))
    # decent in pass-band
    assert_allclose(
        np.mean(data_filt_fft[:, :, pass_mask], 0),
        np.mean(data_fft[:, :, pass_mask], 0),
        rtol=5e-2,
        atol=1e-16,
    )
    # suppression in stop-band
    assert (
        np.mean(data_fft[:, :, stop_mask])
        > np.mean(data_filt_fft[:, :, stop_mask]) * 10
    )

    # smoke test for filtering I/O data (gh-5614)
    temp_fname = tmp_path / "test-epo.fif"
    epochs_orig.save(temp_fname, overwrite=True)
    epochs = mne.read_epochs(temp_fname)
    epochs.filter(None, h_freq)
    assert_allclose(epochs.get_data(), data_filt, atol=1e-17)


def test_epochs_from_annotations():
    """Test epoch instantiation using annotations."""
    raw, events = _get_data()[:2]
    with pytest.raises(
        RuntimeError, match="No usable annotations found in the raw object"
    ):
        Epochs(raw)
    raw.set_annotations(
        mne.annotations_from_events(
            events, raw.info["sfreq"], first_samp=raw.first_samp
        )
    )
    # test on_missing
    with pytest.raises(ValueError, match="No matching annotations"):
        Epochs(raw, event_id="foo")
    # test on_missing warn
    with pytest.warns(match="No matching annotations"):
        Epochs(raw, event_id=["1", "foo"], on_missing="warn")


def test_epochs_hash():
    """Test epoch hashing."""
    raw, events = _get_data()[:2]
    epochs = Epochs(raw, events, event_id, tmin, tmax)
    pytest.raises(RuntimeError, epochs.__hash__)
    epochs = Epochs(raw, events, event_id, tmin, tmax, preload=True)
    assert_equal(hash(epochs), hash(epochs))
    epochs_2 = Epochs(raw, events, event_id, tmin, tmax, preload=True)
    assert_equal(hash(epochs), hash(epochs_2))
    # do NOT use assert_equal here, failing output is terrible
    assert pickle.dumps(epochs) == pickle.dumps(epochs_2)

    epochs_2._data[0, 0, 0] -= 1
    assert hash(epochs) != hash(epochs_2)


def test_event_ordering():
    """Test event order."""
    raw, events = _get_data()[:2]
    events2 = events.copy()[::-1]
    Epochs(raw, events, event_id, tmin, tmax, reject=reject, flat=flat)
    with pytest.warns(RuntimeWarning, match="chronologically"):
        Epochs(raw, events2, event_id, tmin, tmax, reject=reject, flat=flat)
    # Duplicate events should be an error...
    events2 = events[[0, 0]]
    events2[:, 2] = [1, 2]
    pytest.raises(RuntimeError, Epochs, raw, events2, event_id=None)
    # But only if duplicates are actually used by event_id
    assert_equal(len(Epochs(raw, events2, event_id=dict(a=1), preload=True)), 1)


def test_events_type():
    """Test type of events."""
    raw, events = _get_data()[:2]
    events_id = {"A": 1, "B": 2}
    events = (events, events_id)
    with pytest.raises(TypeError, match="events should be a NumPy array"):
        Epochs(raw, events, event_id, tmin, tmax)


def test_rescale():
    """Test rescale."""
    data = np.array([2, 3, 4, 5], float)
    times = np.array([0, 1, 2, 3], float)
    baseline = (0, 2)
    tester = partial(rescale, data=data, times=times, baseline=baseline)
    assert_allclose(tester(mode="mean"), [-1, 0, 1, 2])
    assert_allclose(tester(mode="ratio"), data / 3.0)
    assert_allclose(tester(mode="logratio"), np.log10(data / 3.0))
    assert_allclose(tester(mode="percent"), (data - 3) / 3.0)
    assert_allclose(tester(mode="zscore"), (data - 3) / np.std([2, 3, 4]))
    x = data / 3.0
    x = np.log10(x)
    s = np.std(x[:3])
    assert_allclose(tester(mode="zlogratio"), x / s)


@pytest.mark.parametrize("preload", (True, False))
def test_epochs_baseline_basic(preload, tmp_path):
    """Test baseline and rescaling modes with and without preloading."""
    data = np.array([[2, 3], [2, 3]], float)
    info = create_info(2, 1000.0, ("eeg", "misc"))
    raw = RawArray(data, info)
    events = np.array([[0, 0, 1]])

    epochs = mne.Epochs(raw, events, None, 0, 1e-3, baseline=None, preload=preload)
    epochs.drop_bad()
    epochs_nobl = epochs.copy()
    epochs_data = epochs.get_data(copy=False)
    assert epochs_data.shape == (1, 2, 2)
    expected = data.copy()
    assert_array_equal(epochs_data[0], expected)
    # the baseline period (1 sample here)
    epochs.apply_baseline((0, 0))
    expected[0] = [0, 1]
    if preload:
        assert_allclose(epochs_data[0][0], expected[0])
    else:
        assert_allclose(epochs_data[0][0], expected[1])
    assert_allclose(epochs.get_data()[0], expected, atol=1e-7)
    # entire interval
    epochs.apply_baseline((None, None))
    expected[0] = [-0.5, 0.5]
    assert_allclose(epochs.get_data()[0], expected)

    # Preloading applies baseline correction.
    if preload:
        assert epochs._do_baseline is False
    else:
        assert epochs._do_baseline is True

    # we should not be able to remove baseline correction after the data
    # has been loaded
    epochs.apply_baseline((None, None))
    if preload:
        with pytest.raises(RuntimeError, match="You cannot remove baseline correction"):
            epochs.apply_baseline(None)
    else:
        epochs.apply_baseline(None)
        assert epochs.baseline is None
    # gh-10139
    fname = tmp_path / "test-epo.fif"
    epochs.apply_baseline((None, None))
    assert_allclose(epochs.get_data("eeg").mean(-1), 0, atol=1e-20)
    assert epochs_nobl.baseline is None
    for ep in (epochs, epochs_nobl):
        ep.save(fname, overwrite=True)
        ep = mne.read_epochs(fname, preload=preload)
        ep.apply_baseline((0, 0))
        assert_allclose(ep.get_data("eeg").mean(-1), 0.5, atol=1e-20)
        ep.save(fname, overwrite=True)
        ep = mne.read_epochs(fname, preload=preload)
        assert_allclose(ep.get_data("eeg").mean(-1), 0.5, atol=1e-20)


def test_epochs_bad_baseline():
    """Test Epochs initialization with bad baseline parameters."""
    raw, events = _get_data()[:2]

    with pytest.raises(ValueError, match="interval.*outside of epochs data"):
        epochs = Epochs(raw, events, None, -0.1, 0.3, (-0.2, 0))

    with pytest.raises(ValueError, match="interval.*outside of epochs data"):
        epochs = Epochs(raw, events, None, -0.1, 0.3, (0, 0.4))

    pytest.raises(ValueError, Epochs, raw, events, None, -0.1, 0.3, (0.1, 0))
    pytest.raises(ValueError, Epochs, raw, events, None, 0.1, 0.3, (None, 0))
    pytest.raises(ValueError, Epochs, raw, events, None, -0.3, -0.1, (0, None))
    epochs = Epochs(raw, events, None, 0.1, 0.3, baseline=None)
    epochs.load_data()
    pytest.raises(ValueError, epochs.apply_baseline, (None, 0))
    pytest.raises(ValueError, epochs.apply_baseline, (0, None))
    # put some rescale options here, too
    data = np.arange(100, dtype=float)
    pytest.raises(ValueError, rescale, data, times=data, baseline=(-2, -1))
    rescale(data.copy(), times=data, baseline=(2, 2))  # ok
    pytest.raises(ValueError, rescale, data, times=data, baseline=(2, 1))
    pytest.raises(ValueError, rescale, data, times=data, baseline=(100, 101))


def test_epoch_combine_ids():
    """Test combining event ids in epochs compared to events."""
    raw, events, picks = _get_data()
    epochs = Epochs(
        raw,
        events,
        {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5, "f": 32},
        tmin,
        tmax,
        picks=picks,
        preload=False,
    )
    events_new = merge_events(events, [1, 2], 12)
    epochs_new = combine_event_ids(epochs, ["a", "b"], {"ab": 12})
    assert_equal(epochs_new["ab"]._name, "ab")
    assert_array_equal(events_new, epochs_new.events)
    # should probably add test + functionality for non-replacement XXX


def test_epoch_multi_ids():
    """Test epoch selection via multiple/partial keys."""
    raw, events, picks = _get_data()
    epochs = Epochs(
        raw,
        events,
        {"a/b/a": 1, "a/b/b": 2, "a/c": 3, "b/d": 4, "a_b": 5},
        tmin,
        tmax,
        picks=picks,
        preload=False,
    )
    epochs_regular = epochs["a/b"]
    epochs_reverse = epochs["b/a"]
    epochs_multi = epochs[["a/b/a", "a/b/b"]]
    assert_array_equal(epochs_multi.events, epochs_regular.events)
    assert_array_equal(epochs_reverse.events, epochs_regular.events)
    assert_allclose(epochs_multi.get_data(), epochs_regular.get_data())
    assert_allclose(epochs_reverse.get_data(), epochs_regular.get_data())


def test_read_epochs_bad_events():
    """Test epochs when events are at the beginning or the end of the file."""
    raw, events, picks = _get_data()
    # Event at the beginning
    epochs = Epochs(
        raw,
        np.array([[raw.first_samp, 0, event_id]]),
        event_id,
        tmin,
        tmax,
        picks=picks,
    )
    with pytest.warns(RuntimeWarning, match="empty"):
        evoked = epochs.average()

    epochs = Epochs(
        raw,
        np.array([[raw.first_samp, 0, event_id]]),
        event_id,
        tmin,
        tmax,
        picks=picks,
    )
    assert repr(epochs)  # test repr
    assert epochs._repr_html_()  # test _repr_html_
    with pytest.warns(RuntimeWarning, match="were dropped"):
        epochs.drop_bad()
    assert repr(epochs)
    assert epochs._repr_html_()
    with pytest.raises(RuntimeError, match="empty"):
        evoked = epochs.average()

    # Event at the end
    epochs = Epochs(
        raw, np.array([[raw.last_samp, 0, event_id]]), event_id, tmin, tmax, picks=picks
    )

    with pytest.warns(RuntimeWarning, match="empty"):
        evoked = epochs.average()
    assert evoked


def test_io_epochs_basic(tmp_path):
    """Test epochs from raw files with IO as fif file."""
    raw, events, picks = _get_data(preload=True)
    baseline = (None, 0)
    epochs = Epochs(
        raw, events, event_id, tmin, tmax, picks=picks, baseline=baseline, preload=True
    )
    evoked = epochs.average()
    data = epochs.get_data()

    # Bad tmin/tmax parameters
    with pytest.raises(ValueError, match="tmin has to be less than or equal to tmax"):
        Epochs(raw, events, event_id, tmax, tmin, baseline=None)

    epochs_no_id = Epochs(
        raw, pick_events(events, include=event_id), None, tmin, tmax, picks=picks
    )
    assert_array_equal(data, epochs_no_id.get_data())

    eog_picks = pick_types(
        raw.info, meg=False, eeg=False, stim=False, eog=True, exclude="bads"
    )
    eog_ch_names = [raw.ch_names[k] for k in eog_picks]
    epochs.drop_channels(eog_ch_names)
    assert len(epochs.info["chs"]) == len(epochs.ch_names) == epochs.get_data().shape[1]
    data_no_eog = epochs.get_data()
    assert data.shape[1] == (data_no_eog.shape[1] + len(eog_picks))

    # test decim kwarg
    with pytest.warns(RuntimeWarning, match="aliasing"):
        epochs_dec = Epochs(raw, events, event_id, tmin, tmax, picks=picks, decim=2)

    # decim without
    with epochs_dec.info._unlock():
        epochs_dec.info["lowpass"] = None
    with pytest.warns(RuntimeWarning, match="aliasing"):
        epochs_dec.decimate(2)

    data_dec = epochs_dec.get_data()
    assert_allclose(
        data[:, :, epochs_dec._decim_slice], data_dec, rtol=1e-7, atol=1e-12
    )

    evoked_dec = epochs_dec.average()
    assert_allclose(
        evoked.data[:, epochs_dec._decim_slice], evoked_dec.data, rtol=1e-12, atol=1e-17
    )

    n = evoked.data.shape[1]
    n_dec = evoked_dec.data.shape[1]
    n_dec_min = n // 4
    assert n_dec_min <= n_dec <= n_dec_min + 1
    assert evoked_dec.info["sfreq"] == evoked.info["sfreq"] / 4


@pytest.mark.parametrize(
    "proj",
    [
        pytest.param(True, marks=pytest.mark.slowtest),
        pytest.param("delayed", marks=pytest.mark.slowtest),
        False,
    ],
)
def test_epochs_io_proj(tmp_path, proj):
    """Test epochs I/O with projection."""
    # Test event access on non-preloaded data (#2345)

    # due to reapplication of the proj matrix, this is our quality limit
    # for some tests
    tols = dict(atol=1e-3, rtol=1e-20)

    raw, events, picks = _get_data()
    events[::2, 1] = 1
    events[1::2, 2] = 2
    event_ids = dict(a=1, b=2)
    temp_fname = tmp_path / "test-epo.fif"

    epochs = Epochs(
        raw,
        events,
        event_ids,
        tmin,
        tmax,
        picks=picks,
        proj=proj,
        reject=reject,
        flat=dict(),
        reject_tmin=tmin + 0.01,
        reject_tmax=tmax - 0.01,
    )
    assert_equal(epochs.proj, proj if proj != "delayed" else False)
    data1 = epochs.get_data()
    epochs2 = epochs.copy().apply_proj()
    assert_equal(epochs2.proj, True)
    data2 = epochs2.get_data()
    assert_allclose(data1, data2, **tols)
    epochs.save(temp_fname, overwrite=True)
    epochs_read = read_epochs(temp_fname, preload=False)
    assert_allclose(epochs.get_data(), epochs_read.get_data(), **tols)
    assert_allclose(epochs["a"].get_data(), epochs_read["a"].get_data(), **tols)
    assert_allclose(epochs["b"].get_data(), epochs_read["b"].get_data(), **tols)
    assert epochs.reject is not None
    assert object_diff(epochs.reject, reject) == ""
    assert epochs.flat is None  # empty dict is functionally the same
    assert epochs.reject_tmin == tmin + 0.01
    assert epochs.reject_tmax == tmax - 0.01

    # ensure we don't leak file descriptors
    epochs_read = read_epochs(temp_fname, preload=False)
    epochs_copy = epochs_read.copy()
    del epochs_read
    epochs_copy.get_data()
    del epochs_copy


@pytest.mark.slowtest
@pytest.mark.parametrize("preload", (False, True))
def test_epochs_io_preload(tmp_path, preload):
    """Test epochs I/O with preloading."""
    # due to reapplication of the proj matrix, this is our quality limit
    # for some tests
    tols = dict(atol=1e-3, rtol=1e-20)

    raw, events, picks = _get_data(preload=preload)
    temp_fname = tmp_path / "test-epo.fif"
    temp_fname_no_bl = tmp_path / "test_no_bl-epo.fif"
    baseline = (None, 0)
    with catch_logging() as log:
        epochs = Epochs(
            raw,
            events,
            event_id,
            tmin,
            tmax,
            picks=picks,
            baseline=baseline,
            preload=True,
            verbose=True,
        )
    log = log.getvalue()
    msg = "Not setting metadata"
    assert log.count(msg) == 1, f"\nto find:\n{msg}\n\nlog:\n{log}"
    load_msg = "Loading data for 7 events and 421 original time points ..."
    if preload:
        load_msg = (
            "Using data from preloaded Raw for 7 events and 421 "
            "original time points ..."
        )
    assert log.count(load_msg) == 1, f"\nto find:\n{load_msg}\n\nlog:\n{log}"

    evoked = epochs.average()
    epochs.save(temp_fname, overwrite=True)

    epochs_no_bl = Epochs(
        raw, events, event_id, tmin, tmax, picks=picks, baseline=None, preload=True
    )
    assert epochs_no_bl.baseline is None
    epochs_no_bl.save(temp_fname_no_bl, overwrite=True)

    epochs_read = read_epochs(temp_fname, preload=preload)
    epochs_no_bl.save(temp_fname_no_bl, overwrite=True)
    epochs_read = read_epochs(temp_fname)
    epochs_no_bl_read = read_epochs(temp_fname_no_bl)
    with pytest.raises(ValueError, match="exactly two elements"):
        epochs.apply_baseline(baseline=[1, 2, 3])
    epochs_with_bl = epochs_no_bl_read.copy().apply_baseline(baseline)
    assert isinstance(epochs_with_bl, BaseEpochs)
    assert epochs_with_bl.baseline == (epochs_no_bl_read.tmin, baseline[1])
    assert epochs_no_bl_read.baseline != baseline
    assert str(epochs_read).startswith("<Epochs")

    epochs_no_bl_read.apply_baseline(baseline)
    assert_array_equal(epochs_no_bl_read.times, epochs.times)
    assert_array_almost_equal(epochs_read.get_data(), epochs.get_data())
    assert_array_almost_equal(epochs.get_data(), epochs_no_bl_read.get_data())
    assert_array_equal(epochs_read.times, epochs.times)
    assert_array_almost_equal(epochs_read.average().data, evoked.data)
    assert_equal(epochs_read.proj, epochs.proj)
    bmin, bmax = epochs.baseline
    if bmin is None:
        bmin = epochs.times[0]
    if bmax is None:
        bmax = epochs.times[-1]
    baseline = (bmin, bmax)
    assert_array_almost_equal(epochs_read.baseline, baseline)
    assert_array_almost_equal(epochs_read.tmin, epochs.tmin, 2)
    assert_array_almost_equal(epochs_read.tmax, epochs.tmax, 2)
    assert_equal(epochs_read.event_id, epochs.event_id)

    epochs.event_id.pop("1")
    epochs.event_id.update({"a:a": 1})  # test allow for ':' in key
    fname_temp = tmp_path / "foo-epo.fif"
    epochs.save(fname_temp, overwrite=True)
    epochs_read = read_epochs(fname_temp, preload=preload)
    assert_equal(epochs_read.event_id, epochs.event_id)
    assert_equal(epochs_read["a:a"].average().comment, "a:a")

    # now use a baseline, crop it out, and I/O round trip afterward
    assert epochs.times[0] < 0
    assert epochs.times[-1] > 0
    epochs.apply_baseline((None, 0))
    baseline_before_crop = (epochs.times[0], 0)
    epochs.crop(1.0 / epochs.info["sfreq"], None)
    # baseline shouldn't be modified by crop()
    assert epochs.baseline == baseline_before_crop
    epochs.save(fname_temp, overwrite=True)
    epochs_read = read_epochs(fname_temp, preload=preload)
    assert_allclose(epochs_read.baseline, baseline_before_crop)

    assert_allclose(
        epochs.get_data(), epochs_read.get_data(), rtol=6e-4
    )  # XXX this rtol should be better...?
    del epochs, epochs_read

    # add reject here so some of the epochs get dropped
    epochs = Epochs(raw, events, event_id, tmin, tmax, picks=picks, reject=reject)
    epochs.save(temp_fname, overwrite=True)
    # ensure bad events are not saved
    epochs_read3 = read_epochs(temp_fname, preload=preload)
    assert_array_equal(epochs_read3.events, epochs.events)
    data = epochs.get_data()
    assert epochs_read3.events.shape[0] == data.shape[0]

    # test copying loaded one (raw property)
    epochs_read4 = epochs_read3.copy()
    assert_array_almost_equal(epochs_read4.get_data(), data)
    # test equalizing loaded one (drop_log property)
    epochs_read4.equalize_event_counts(epochs.event_id)

    epochs.drop([1, 2], reason="can we recover orig ID?")
    epochs.save(temp_fname, overwrite=True)
    epochs_read5 = read_epochs(temp_fname, preload=preload)
    assert_array_equal(epochs_read5.selection, epochs.selection)
    assert_equal(len(epochs_read5.selection), len(epochs_read5.events))
    assert epochs_read5.drop_log == epochs.drop_log

    if preload:
        # Test that one can drop channels on read file
        epochs_read5.drop_channels(epochs_read5.ch_names[:1])

    # test warnings on bad filenames
    epochs_badname = tmp_path / "test-bad-name.fif.gz"
    with pytest.warns(RuntimeWarning, match="-epo.fif"):
        epochs.save(epochs_badname, overwrite=True)
    with pytest.warns(RuntimeWarning, match="-epo.fif"):
        read_epochs(epochs_badname, preload=preload)

    # test loading epochs with missing events
    epochs = Epochs(
        raw, events, dict(foo=1, bar=999), tmin, tmax, picks=picks, on_missing="ignore"
    )
    epochs.save(temp_fname, overwrite=True)
    _assert_splits(temp_fname, 0, np.inf)
    epochs_read = read_epochs(temp_fname, preload=preload)
    assert_allclose(epochs.get_data(), epochs_read.get_data(), **tols)
    assert_array_equal(epochs.events, epochs_read.events)
    assert_equal(
        set(epochs.event_id.keys()), {str(x) for x in epochs_read.event_id.keys()}
    )

    # test saving split epoch files
    split_size = "7MB"
    # ensure that we're in a position where just the data itself could fit
    # if that were all that we saved ...
    split_size_bytes = _get_split_size(split_size)
    assert epochs.get_data().nbytes // 2 < split_size_bytes
    epochs.save(temp_fname, split_size=split_size, overwrite=True)
    # ... but we correctly account for the other stuff we need to write,
    # so end up with two files ...
    _assert_splits(temp_fname, 1, split_size_bytes)
    epochs_read = read_epochs(temp_fname, preload=preload)
    # ... and none of the files exceed our limit.
    _assert_splits(temp_fname, 1, split_size_bytes)
    assert_allclose(epochs.get_data(), epochs_read.get_data(), **tols)
    assert_array_equal(epochs.events, epochs_read.events)
    assert_array_equal(epochs.selection, epochs_read.selection)
    assert epochs.drop_log == epochs_read.drop_log

    # Test that having a single time point works
    assert epochs.baseline is not None
    baseline_before_crop = epochs.baseline
    epochs.load_data().crop(0, 0)
    assert epochs.baseline == baseline_before_crop
    assert_equal(len(epochs.times), 1)
    assert_equal(epochs.get_data().shape[-1], 1)
    epochs.save(temp_fname, overwrite=True)
    epochs_read = read_epochs(temp_fname, preload=preload)
    assert_equal(len(epochs_read.times), 1)
    assert_equal(epochs.get_data().shape[-1], 1)


@pytest.fixture(scope="session")
def epochs_factory():
    """Create fake Epochs object.

    Metadata and concat address gh-5102, gh-7897.
    """

    def factory(n_epochs, metadata=False, concat=False):
        if metadata:
            pytest.importorskip("pandas")
        # See gh-5102
        n_ch, fs = 100, 1000.0
        n_times = int(round(fs * (n_epochs + 1)))
        raw_data = np.random.RandomState(0).randn(n_ch, n_times)
        raw = mne.io.RawArray(raw_data, mne.create_info(n_ch, fs))
        events = mne.make_fixed_length_events(raw, 1)
        epochs = mne.Epochs(raw, events)
        if metadata:
            from pandas import DataFrame

            junk = ["*" * 10000 for _ in range(len(events))]
            metadata = DataFrame(
                {
                    "event_time": events[:, 0] / raw.info["sfreq"],
                    "trial_number": range(len(events)),
                    "junk": junk,
                }
            )
            epochs.metadata = metadata
        epochs.drop_bad()
        if concat:
            epochs = concatenate_epochs([epochs[ii] for ii in range(len(epochs))])
        assert len(epochs) == n_epochs
        return epochs

    return factory


@pytest.fixture(
    params=[
        ("1.5MB", 8, True, True, 6),
        ("1.5MB", 8, True, False, 6),
        ("1.5MB", 8, False, True, 6),
        ("1.5MB", 8, False, False, 6),
        ("3MB", 14, True, True, 3),
        ("3MB", 14, True, False, 3),
        ("3MB", 14, False, True, 2),
        ("3MB", 14, False, False, 2),
        ("3MB", 15, False, False, 3),
        ("3MB", 18, True, True, 3),
        ("3MB", 18, True, False, 3),
        ("3MB", 18, False, True, 3),
        ("3MB", 18, False, False, 3),
    ]
)
def epochs_to_split(request, epochs_factory):
    """Epochs tailored to produce specific number of splits when saving.

    We're specifically interested in boundary cases, when a small size
    excess triggers creation of a new split: gh-7897

    """
    split_size, n_epochs, metadata, concat, n_files = request.param
    epochs = epochs_factory(n_epochs, metadata, concat)
    return epochs, split_size, n_files


@pytest.mark.parametrize("preload", [True, False], ids=["preload", "no_preload"])
def test_split_saving_and_loading_back(tmp_path, epochs_to_split, preload):
    """Test saving split epochs and loading them back.

    In particular, check events after loading splits to test against gh-5102.

    """
    epochs, split_size, n_files = epochs_to_split
    epochs_data = epochs.get_data()
    fname = tmp_path / "test-epo.fif"
    got_size = _get_split_size(split_size)

    epochs.save(fname, split_size=split_size, overwrite=True)
    epochs2 = mne.read_epochs(fname, preload=preload)

    _assert_splits(fname, n_files, got_size)
    assert not fname.with_name(f"{fname.stem}-{n_files + 1}{fname.suffix}").is_file()
    assert_allclose(epochs2.get_data(), epochs_data)
    assert_array_equal(epochs.events, epochs2.events)


@pytest.mark.parametrize(
    "split_naming, dst_fname, split_fname_fn, check_bids",
    [
        (
            "neuromag",
            "test_epo.fif",
            lambda i: f"test_epo-{i}.fif" if i else "test_epo.fif",
            False,
        ),
        (
            "bids",
            Path("sub-01") / "meg" / "sub-01_epo.fif",
            lambda i: Path("sub-01") / "meg" / f"sub-01_split-{i + 1:02d}_epo.fif",
            True,
        ),
        (
            "bids",
            "a_b-epo.fif",
            # Merely stating the fact:
            lambda i: f"a_split-{i + 1:02d}_b-epo.fif",
            False,
        ),
    ],
    ids=["neuromag", "bids", "mix"],
)
def test_split_naming(
    tmp_path, epochs_to_split, split_naming, dst_fname, split_fname_fn, check_bids
):
    """Test naming of the split files."""
    epochs, split_size, n_files = epochs_to_split
    dst_fpath = tmp_path / dst_fname
    save_kwargs = {"split_size": split_size, "split_naming": split_naming}
    # we don't test for reserved files as it's not implemented here
    if dst_fpath.parent != tmp_path:
        dst_fpath.parent.mkdir(parents=True)

    split_fnames = epochs.save(dst_fpath, verbose=True, **save_kwargs)

    # check that the filenames match the intended pattern
    assert len(list(dst_fpath.parent.iterdir())) == n_files
    assert not (tmp_path / split_fname_fn(n_files)).is_file()
    want_paths = [tmp_path / split_fname_fn(i) for i in range(n_files)]
    assert split_fnames == want_paths
    for want_path in want_paths:
        assert want_path.is_file()

    if not check_bids:
        return
    # gh-12451
    # If we load sub-01_split-01_epo.fif we should then we shouldn't
    # write sub-01_split-01_split-01_epo.fif
    mne_bids = pytest.importorskip("mne_bids")
    # Let's try to prevent people from making a mistake
    bids_path = mne_bids.BIDSPath(
        root=tmp_path,
        subject="01",
        datatype="meg",
        split="01",
        suffix="epo",
        extension=".fif",
        check=False,
    )
    assert bids_path.fpath.is_file(), bids_path.fpath
    for want_path in want_paths:
        want_path.unlink()
    assert not bids_path.fpath.is_file()
    with pytest.raises(ValueError, match="Passing a BIDSPath"):
        epochs.save(bids_path, verbose=True, **save_kwargs)
    bad_path = bids_path.fpath.parent / (bids_path.fpath.stem[:-3] + "split-01_epo.fif")
    assert str(bad_path).count("_split-01") == 2
    assert not bad_path.is_file(), bad_path
    bids_path.split = None
    split_fnames = epochs.save(bids_path, verbose=True, **save_kwargs)
    for split_fname in split_fnames:
        assert split_fname.is_file()


@pytest.mark.parametrize(
    "dst_fname, split_naming, split_1_fname",
    [
        ("test_epo.fif", "neuromag", "test_epo-1.fif"),
        ("test_epo.fif", "bids", "test_split-01_epo.fif"),
    ],
)
def test_saved_fname_no_splitting(
    tmp_path, epochs_factory, dst_fname, split_naming, split_1_fname
):
    """Test saved fname when splitting not needed.

    - Check "zero-th split" doesn't get the split suffix
    - Check "first split" isn't produced

    """
    epochs = epochs_factory(n_epochs=9)
    dst_fpath = tmp_path / dst_fname
    split_1_fpath = tmp_path / split_1_fname

    filenames = epochs.save(dst_fpath, split_naming=split_naming, verbose=True)
    assert filenames == [dst_fpath]

    assert dst_fpath.is_file()
    assert not split_1_fpath.is_file()


@pytest.mark.parametrize(
    "epochs_to_split",
    [
        ("3MB", 18, False, False, 3),
        pytest.param(
            ("2GB", 18, False, False, 1),
            marks=pytest.mark.xfail(reason="No check when not splitting"),
        ),
    ],
    indirect=True,
)
@pytest.mark.parametrize(
    "dst_fname",
    [
        "test-epo.fif",
        pytest.param(
            "a_b_c-epo.fif",
            marks=pytest.mark.xfail(reason="No check for several bids clauses"),
        ),
    ],
)
def test_bids_splits_fail_for_bad_fname_ending(epochs_to_split, dst_fname, tmp_path):
    """Make sure split_naming=bids is only used with bids endings.

    Non-bids endings can cause surprising split names, e.g. test-epo.fif
    producing splits _split-01_test-epo.fif.

    """
    epochs, split_size, _ = epochs_to_split
    dst_fpath = tmp_path / dst_fname
    save_kwargs = {"split_naming": "bids", "split_size": split_size}

    with pytest.raises(ValueError, match=".* must end with an underscore"):
        epochs.save(dst_fpath, verbose=True, **save_kwargs)


@pytest.mark.parametrize(
    "epochs_to_split", [("3MB", 18, False, False, 3)], indirect=True
)
@pytest.mark.parametrize(
    "split_naming, dst_fname, existing_fname",
    [
        ("neuromag", "test-epo.fif", "test-epo.fif"),
        ("neuromag", "test-epo.fif", "test-epo-1.fif"),
        ("bids", "test_epo.fif", "test_epo.fif"),
        ("bids", "test_epo.fif", "test_split-01_epo.fif"),
        ("bids", "test_epo.fif", "test_split-02_epo.fif"),
    ],
)
def test_splits_overwrite(
    tmp_path, epochs_to_split, split_naming, dst_fname, existing_fname
):
    """Check exception is raised when overwriting without explicit flag.

    Check a case when overwrite occurs because of a split.
    """
    dst_fpath = tmp_path / dst_fname
    epochs, split_size, _ = epochs_to_split
    save_kwargs = {"split_naming": split_naming, "split_size": split_size}

    (tmp_path / existing_fname).touch()

    with pytest.raises(FileExistsError, match="Destination file"):
        epochs.save(dst_fpath, verbose=True, overwrite=False, **save_kwargs)


@pytest.mark.slowtest
def test_split_many_reset(tmp_path):
    """Test splitting with many events and using reset."""
    data = np.zeros((1000, 1, 1024))  # 1 ch, 1024 samples
    assert data[0, 0].nbytes == 8192  # 8 kB per epoch
    info = mne.create_info(1, 1000.0, "eeg")
    selection = np.arange(len(data)) + 100000
    epochs = EpochsArray(data, info, tmin=0.0, selection=selection)
    assert len(epochs.drop_log) == 101000
    assert len(epochs) == len(data) == len(epochs.events)
    fname = tmp_path / "temp-epo.fif"
    for split_size in ("0.5MB", "1MB", "2MB"):  # tons of overhead from sel
        with pytest.raises(ValueError, match="too small to safely"):
            epochs.save(fname, split_size=split_size, verbose="debug")
    with pytest.raises(ValueError, match="would result in writing"):  # ~200
        epochs.save(fname, split_size="2.27MB", verbose="debug")
    with pytest.warns(RuntimeWarning, match="writing overhead"):
        epochs.save(fname, split_size="3MB", verbose="debug")
    epochs_read = read_epochs(fname)
    assert_allclose(epochs.get_data(), epochs_read.get_data())
    assert epochs.drop_log == epochs_read.drop_log
    mb = 3 * 1024 * 1024
    _assert_splits(fname, 6, mb)
    # reset, then it should work
    fname = tmp_path / "temp-reset-epo.fif"
    epochs.reset_drop_log_selection()
    epochs.save(fname, split_size=split_size, verbose="debug")
    _assert_splits(fname, 4, mb)
    epochs_read = read_epochs(fname)
    assert_allclose(epochs.get_data(), epochs_read.get_data())


def _assert_splits(fname, n, size):
    __tracebackhide__ = True
    assert n >= 0
    next_fnames = [fname] + [
        fname.with_name(f"{fname.stem}-{ii}{fname.suffix}") for ii in range(1, n + 2)
    ]
    bad_fname = next_fnames.pop(-1)
    for ii, this_fname in enumerate(next_fnames[:-1]):
        assert this_fname.is_file(), f"Missing file: {this_fname}"
        with open(this_fname) as fid:
            fid.seek(0, 2)
            file_size = fid.tell()
        min_ = 0.1 if ii < len(next_fnames) - 1 else 0.1
        assert size * min_ < file_size <= size, f"{this_fname}"
    assert not bad_fname.is_file(), f"Errantly wrote {bad_fname}"


def test_epochs_proj(tmp_path):
    """Test handling projection (apply proj in Raw or in Epochs)."""
    raw, events, picks = _get_data()
    exclude = raw.info["bads"] + ["MEG 2443", "EEG 053"]  # bads + 2 more
    this_picks = pick_types(
        raw.info, meg=True, eeg=False, stim=True, eog=True, exclude=exclude
    )
    epochs = Epochs(raw, events[:4], event_id, tmin, tmax, picks=this_picks, proj=True)
    assert all(p["active"] is True for p in epochs.info["projs"])
    evoked = epochs.average()
    assert all(p["active"] is True for p in evoked.info["projs"])
    data = epochs.get_data()

    raw_proj = read_raw_fif(raw_fname).apply_proj()
    epochs_no_proj = Epochs(
        raw_proj, events[:4], event_id, tmin, tmax, picks=this_picks, proj=False
    )

    data_no_proj = epochs_no_proj.get_data()
    assert all(p["active"] is True for p in epochs_no_proj.info["projs"])
    evoked_no_proj = epochs_no_proj.average()
    assert all(p["active"] is True for p in evoked_no_proj.info["projs"])
    assert epochs_no_proj.proj is True  # as projs are active from Raw

    assert_array_almost_equal(data, data_no_proj, decimal=8)

    # make sure we can exclude avg ref
    this_picks = pick_types(
        raw.info, meg=True, eeg=True, stim=True, eog=True, exclude=exclude
    )
    epochs = Epochs(raw, events[:4], event_id, tmin, tmax, picks=this_picks, proj=True)
    epochs.set_eeg_reference(projection=True).apply_proj()
    assert _has_eeg_average_ref_proj(epochs.info)
    epochs = Epochs(raw, events[:4], event_id, tmin, tmax, picks=this_picks, proj=True)
    assert not _has_eeg_average_ref_proj(epochs.info)

    # make sure we don't add avg ref when a custom ref has been applied
    with raw.info._unlock():
        raw.info["custom_ref_applied"] = True
    epochs = Epochs(raw, events[:4], event_id, tmin, tmax, picks=this_picks, proj=True)
    assert not _has_eeg_average_ref_proj(epochs.info)

    # From GH#2200:
    # This has no problem
    proj = raw.info["projs"]
    epochs = Epochs(raw, events[:4], event_id, tmin, tmax, picks=this_picks, proj=False)
    with epochs.info._unlock():
        epochs.info["projs"] = []
    data = epochs.copy().add_proj(proj).apply_proj().get_data()
    # save and reload data
    fname_epo = tmp_path / "temp-epo.fif"
    epochs.save(fname_epo, overwrite=True)  # Save without proj added
    epochs_read = read_epochs(fname_epo)
    epochs_read.add_proj(proj)
    epochs_read.apply_proj()  # This used to bomb
    data_2 = epochs_read.get_data()  # Let's check the result
    assert_allclose(data, data_2, atol=1e-15, rtol=1e-3)

    # adding EEG ref (GH #2727)
    raw = read_raw_fif(raw_fname)
    raw.add_proj([], remove_existing=True)
    raw.info["bads"] = ["MEG 2443", "EEG 053"]
    picks = pick_types(
        raw.info, meg=False, eeg=True, stim=True, eog=False, exclude="bads"
    )
    epochs = Epochs(
        raw, events, event_id, tmin, tmax, proj=True, picks=picks, preload=True
    )
    epochs.pick(["EEG 001", "EEG 002"])
    assert_equal(len(epochs), 7)  # sufficient for testing
    temp_fname = tmp_path / "test-epo.fif"
    epochs.save(temp_fname, overwrite=True)
    for preload in (True, False):
        epochs = read_epochs(temp_fname, proj=False, preload=preload)
        epochs.set_eeg_reference(projection=True).apply_proj()
        assert_allclose(epochs.get_data().mean(axis=1), 0, atol=1e-15)
        epochs = read_epochs(temp_fname, proj=False, preload=preload)
        epochs.set_eeg_reference(projection=True)
        pytest.raises(
            AssertionError,
            assert_allclose,
            epochs.get_data().mean(axis=1),
            0.0,
            atol=1e-15,
        )
        epochs.apply_proj()
        assert_allclose(epochs.get_data().mean(axis=1), 0, atol=1e-15)


def test_evoked_arithmetic():
    """Test arithmetic of evoked data."""
    raw, events, picks = _get_data()
    epochs1 = Epochs(raw, events[:4], event_id, tmin, tmax, picks=picks)
    evoked1 = epochs1.average()
    epochs2 = Epochs(raw, events[4:8], event_id, tmin, tmax, picks=picks)
    evoked2 = epochs2.average()
    epochs = Epochs(raw, events[:8], event_id, tmin, tmax, picks=picks)
    evoked = epochs.average()
    evoked_avg = combine_evoked([evoked1, evoked2], weights="nave")
    assert_array_equal(evoked.data, evoked_avg.data)
    assert_array_equal(evoked.times, evoked_avg.times)
    assert_equal(evoked_avg.nave, evoked1.nave + evoked2.nave)


def test_evoked_io_from_epochs(tmp_path):
    """Test IO of evoked data made from epochs."""
    raw, events, picks = _get_data()
    with raw.info._unlock():
        raw.info["lowpass"] = 40  # avoid aliasing warnings
    # offset our tmin so we don't get exactly a zero value when decimating
    with catch_logging() as log:
        epochs = Epochs(
            raw,
            events[:4],
            event_id,
            tmin + 0.011,
            tmax,
            picks=picks,
            decim=5,
            preload=True,
            verbose=True,
        )
    log = log.getvalue()
    load_msg = (
        "Loading data for 1 events and 415 original time points "
        "(prior to decimation) ..."
    )
    assert log.count(load_msg) == 1, f"\nto find:\n{load_msg}\n\nlog:\n{log}"
    evoked = epochs.average()
    with evoked.info._unlock():
        # Test that empty string shortcuts to None.
        evoked.info["proj_name"] = ""
    fname_temp = tmp_path / "evoked-ave.fif"
    evoked.save(fname_temp)
    evoked2 = read_evokeds(fname_temp)[0]
    assert_equal(evoked2.info["proj_name"], None)
    assert_allclose(evoked.data, evoked2.data, rtol=1e-4, atol=1e-20)
    assert_allclose(
        evoked.times, evoked2.times, rtol=1e-4, atol=1 / evoked.info["sfreq"]
    )

    # now let's do one with negative time
    baseline = (0.1, 0.2)
    epochs = Epochs(
        raw, events[:4], event_id, 0.1, tmax, picks=picks, baseline=baseline, decim=5
    )
    evoked = epochs.average()
    assert_allclose(evoked.baseline, baseline)
    evoked.save(fname_temp, overwrite=True)
    evoked2 = read_evokeds(fname_temp)[0]
    assert_allclose(evoked.data, evoked2.data, rtol=1e-4, atol=1e-20)
    assert_allclose(evoked.times, evoked2.times, rtol=1e-4, atol=1e-20)
    assert_allclose(evoked.baseline, baseline)

    # should be equivalent to a cropped original
    baseline = (0.1, 0.2)
    epochs = Epochs(
        raw, events[:4], event_id, -0.2, tmax, picks=picks, baseline=baseline, decim=5
    )
    evoked = epochs.average()
    evoked.crop(0.099, None)
    assert_allclose(evoked.data, evoked2.data, rtol=1e-4, atol=1e-20)
    assert_allclose(evoked.times, evoked2.times, rtol=1e-4, atol=1e-20)
    assert_allclose(evoked.baseline, baseline)

    # should work when one channel type is changed to a non-data ch
    picks = pick_types(raw.info, meg=True, eeg=True)
    epochs = Epochs(
        raw, events[:4], event_id, -0.2, tmax, picks=picks, baseline=(0.1, 0.2), decim=5
    )
    epochs.set_channel_types({epochs.ch_names[0]: "syst"}, on_unit_change="ignore")
    evokeds = list()
    for picks in (None, "all"):
        evoked = epochs.average(picks)
        evokeds.append(evoked)
        evoked.save(fname_temp, overwrite=True)
        evoked2 = read_evokeds(fname_temp)[0]
        start = 1 if picks is None else 0
        for ev in (evoked, evoked2):
            assert ev.ch_names == epochs.ch_names[start:]
            assert_allclose(ev.data, epochs.get_data().mean(0)[start:])
    with pytest.raises(ValueError, match=".*nchan.* must match"):
        write_evokeds(fname_temp, evokeds, overwrite=True)


def test_evoked_standard_error(tmp_path):
    """Test calculation and read/write of standard error."""
    raw, events, picks = _get_data()
    epochs = Epochs(raw, events[:4], event_id, tmin, tmax, picks=picks)
    evoked = [epochs.average(), epochs.standard_error()]
    write_evokeds(tmp_path / "evoked-ave.fif", evoked)
    evoked2 = read_evokeds(tmp_path / "evoked-ave.fif", [0, 1])
    evoked3 = [
        read_evokeds(tmp_path / "evoked-ave.fif", "1"),
        read_evokeds(tmp_path / "evoked-ave.fif", "1", kind="standard_error"),
    ]
    for evoked_new in [evoked2, evoked3]:
        assert evoked_new[0]._aspect_kind == FIFF.FIFFV_ASPECT_AVERAGE
        assert evoked_new[0].kind == "average"
        assert evoked_new[1]._aspect_kind == FIFF.FIFFV_ASPECT_STD_ERR
        assert evoked_new[1].kind == "standard_error"
        for ave, ave2 in zip(evoked, evoked_new):
            assert_array_almost_equal(ave.data, ave2.data)
            assert_array_almost_equal(ave.times, ave2.times)
            assert ave.nave == ave2.nave
            assert ave._aspect_kind == ave2._aspect_kind
            assert ave.kind == ave2.kind
            assert ave.last == ave2.last
            assert ave.first == ave2.first


def test_reject_epochs(tmp_path):
    """Test of epochs rejection."""
    temp_fname = tmp_path / "test-epo.fif"
    raw, events, picks = _get_data()
    events1 = events[events[:, 2] == event_id]
    epochs = Epochs(raw, events1, event_id, tmin, tmax, reject=reject, flat=flat)
    pytest.raises(RuntimeError, len, epochs)
    n_events = len(epochs.events)
    data = epochs.get_data()
    n_clean_epochs = len(data)
    # Should match
    # mne_process_raw --raw test_raw.fif --projoff \
    #   --saveavetag -ave --ave test.ave --filteroff
    assert n_events > n_clean_epochs
    assert n_clean_epochs == 3
    assert epochs.drop_log == (
        (),
        (),
        (),
        ("MEG 2443",),
        ("MEG 2443",),
        ("MEG 2443",),
        ("MEG 2443",),
    )

    # Ensure epochs are not dropped based on a bad channel
    raw_2 = raw.copy()
    raw_2.info["bads"] = ["MEG 2443"]
    reject_crazy = dict(grad=1000e-15, mag=4e-15, eeg=80e-9, eog=150e-9)
    epochs = Epochs(
        raw_2, events1, event_id, tmin, tmax, reject=reject_crazy, flat=flat
    )
    with pytest.warns(RuntimeWarning, match="were dropped"):
        epochs.drop_bad()

    assert all("MEG 2442" in e for e in epochs.drop_log)
    assert all("MEG 2443" not in e for e in epochs.drop_log)

    # Invalid reject_tmin/reject_tmax/detrend
    pytest.raises(
        ValueError,
        Epochs,
        raw,
        events1,
        event_id,
        tmin,
        tmax,
        reject_tmin=1.0,
        reject_tmax=0,
    )
    pytest.raises(
        ValueError,
        Epochs,
        raw,
        events1,
        event_id,
        tmin,
        tmax,
        reject_tmin=tmin - 1,
        reject_tmax=1.0,
    )
    pytest.raises(
        ValueError,
        Epochs,
        raw,
        events1,
        event_id,
        tmin,
        tmax,
        reject_tmin=0.0,
        reject_tmax=tmax + 1,
    )

    epochs = Epochs(
        raw,
        events1,
        event_id,
        tmin,
        tmax,
        picks=picks,
        reject=reject,
        flat=flat,
        reject_tmin=0.0,
        reject_tmax=0.1,
    )
    data = epochs.get_data()
    n_clean_epochs = len(data)
    assert n_clean_epochs == 7
    assert len(epochs) == 7
    assert epochs.times[epochs._reject_time][0] >= 0.0
    assert epochs.times[epochs._reject_time][-1] <= 0.1

    # Invalid data for _is_good_epoch function
    epochs = Epochs(raw, events1, event_id, tmin, tmax)
    assert epochs._is_good_epoch(None) == (False, ("NO_DATA",))
    assert epochs._is_good_epoch(np.zeros((1, 1))) == (False, ("TOO_SHORT",))
    data = epochs[0].get_data()[0]
    assert epochs._is_good_epoch(data) == (True, None)

    # Check that reject_tmin and reject_tmax are being adjusted for small time
    # inaccuracies due to sfreq
    epochs = Epochs(
        raw=raw,
        events=events1,
        event_id=event_id,
        tmin=tmin,
        tmax=tmax,
        reject_tmin=tmin,
        reject_tmax=tmax,
    )
    assert epochs.tmin != tmin
    assert epochs.tmax != tmax
    assert np.isclose(epochs.tmin, epochs.reject_tmin)
    assert np.isclose(epochs.tmax, epochs.reject_tmax)
    epochs.save(temp_fname, overwrite=True)
    read_epochs(temp_fname)

    # Ensure repeated rejection works, even if applied to only a subset of the
    # previously-used channel types
    epochs = Epochs(raw, events1, event_id, tmin, tmax, reject=reject, flat=flat)

    new_reject = reject.copy()
    new_flat = flat.copy()
    del new_reject["grad"], new_reject["eeg"], new_reject["eog"]
    del new_flat["mag"]

    # No changes expected
    epochs_cleaned = epochs.copy().drop_bad(reject=new_reject, flat=new_flat)
    assert epochs_cleaned.reject == epochs.reject
    assert epochs_cleaned.flat == epochs.flat

    new_reject["mag"] /= 2
    new_flat["grad"] *= 2
    # Only the newly-provided thresholds should be updated, the existing ones
    # should be kept
    with pytest.warns(RuntimeWarning, match="were dropped"):
        epochs_cleaned = epochs.copy().drop_bad(reject=new_reject, flat=new_flat)
    assert epochs_cleaned.reject == dict(
        mag=new_reject["mag"], grad=reject["grad"], eeg=reject["eeg"], eog=reject["eog"]
    )
    assert epochs_cleaned.flat == dict(grad=new_flat["grad"], mag=flat["mag"])


@testing.requires_testing_data
def test_callable_reject():
    """Test using a callable for rejection."""
    raw = read_raw_fif(fname_raw_testing, preload=True)
    raw.crop(0, 5)
    raw.del_proj()
    chans = raw.info["ch_names"][-6:-1]
    raw.pick(chans)
    data = raw.get_data()

    # Add some artifacts
    new_data = data
    new_data[0, 180:200] *= 1e7
    new_data[0, 610:880] += 1e-3
    edit_raw = mne.io.RawArray(new_data, raw.info)

    events = mne.make_fixed_length_events(edit_raw, id=1, duration=1.0, start=0)
    epochs = mne.Epochs(edit_raw, events, tmin=0, tmax=1, baseline=None, preload=True)
    assert len(epochs) == 5

    epochs = mne.Epochs(
        edit_raw,
        events,
        tmin=0,
        tmax=1,
        baseline=None,
        preload=True,
    )
    epochs.drop_bad(
        reject=dict(eeg=lambda x: ((np.median(x, axis=1) > 1e-3).any(), "eeg median"))
    )

    assert epochs.drop_log[2] == ("eeg median",)

    epochs = mne.Epochs(
        edit_raw,
        events,
        tmin=0,
        tmax=1,
        baseline=None,
        preload=True,
    )
    epochs.drop_bad(
        reject=dict(eeg=lambda x: ((np.max(x, axis=1) > 1).any(), ("eeg max",)))
    )

    assert epochs.drop_log[0] == ("eeg max",)

    def reject_criteria(x):
        max_condition = np.max(x, axis=1) > 1e-2
        median_condition = np.median(x, axis=1) > 1e-4
        return (max_condition.any() or median_condition.any()), "eeg max or median"

    epochs = mne.Epochs(
        edit_raw,
        events,
        tmin=0,
        tmax=1,
        baseline=None,
        preload=True,
    )
    epochs.drop_bad(reject=dict(eeg=reject_criteria))

    assert epochs.drop_log[0] == ("eeg max or median",) and epochs.drop_log[2] == (
        "eeg max or median",
    )

    # Test reasons must be str or tuple of str
    with pytest.raises(
        TypeError,
        match=r".* must be an instance of str, got <class 'int'> instead.",
    ):
        epochs = mne.Epochs(
            edit_raw,
            events,
            tmin=0,
            tmax=1,
            baseline=None,
            preload=True,
        )
        epochs.drop_bad(
            reject=dict(
                eeg=lambda x: ((np.median(x, axis=1) > 1e-3).any(), ("eeg median", 2))
            )
        )


def test_preload_epochs():
    """Test preload of epochs."""
    raw, events, picks = _get_data()
    epochs_preload = Epochs(
        raw,
        events[:16],
        event_id,
        tmin,
        tmax,
        picks=picks,
        preload=True,
        reject=reject,
        flat=flat,
    )
    data_preload = epochs_preload.get_data()

    epochs = Epochs(
        raw,
        events[:16],
        event_id,
        tmin,
        tmax,
        picks=picks,
        preload=False,
        reject=reject,
        flat=flat,
    )
    data = epochs.get_data()
    assert_array_equal(data_preload, data)
    assert_array_almost_equal(epochs_preload.average().data, epochs.average().data, 18)


def test_indexing_slicing():
    """Test of indexing and slicing operations."""
    raw, events, picks = _get_data()
    epochs = Epochs(
        raw, events[:20], event_id, tmin, tmax, picks=picks, reject=reject, flat=flat
    )

    data_normal = epochs.get_data()

    n_good_events = data_normal.shape[0]

    # indices for slicing
    start_index = 1
    end_index = n_good_events - 1

    assert (end_index - start_index) > 0

    for preload in [True, False]:
        epochs2 = Epochs(
            raw,
            events[:20],
            event_id,
            tmin,
            tmax,
            picks=picks,
            preload=preload,
            reject=reject,
            flat=flat,
        )

        if not preload:
            epochs2.drop_bad()

        # using slicing
        epochs2_sliced = epochs2[start_index:end_index]

        data_epochs2_sliced = epochs2_sliced.get_data()
        assert_array_equal(data_epochs2_sliced, data_normal[start_index:end_index])

        # using indexing
        pos = 0
        for idx in range(start_index, end_index):
            data = epochs2_sliced[pos].get_data()
            assert_array_equal(data[0], data_normal[idx])
            pos += 1

        # using indexing with an int
        data = epochs2[data_epochs2_sliced.shape[0]].get_data()
        assert_array_equal(data, data_normal[[idx]])

        # using indexing with an array
        idx = rng.randint(0, data_epochs2_sliced.shape[0], 10)
        data = epochs2[idx].get_data()
        assert_array_equal(data, data_normal[idx])

        # using indexing with a list of indices
        idx = [0]
        data = epochs2[idx].get_data()
        assert_array_equal(data, data_normal[idx])
        idx = [0, 1]
        data = epochs2[idx].get_data()
        assert_array_equal(data, data_normal[idx])


def test_comparision_with_c():
    """Test of average obtained vs C code."""
    raw, events = _get_data()[:2]
    c_evoked = read_evokeds(evoked_nf_name, condition=0)
    epochs = Epochs(
        raw, events, event_id, tmin, tmax, baseline=None, preload=True, proj=False
    )
    evoked = epochs.set_eeg_reference(projection=True).apply_proj().average()
    sel = pick_channels(c_evoked.ch_names, evoked.ch_names)
    evoked_data = evoked.data
    c_evoked_data = c_evoked.data[sel]

    assert evoked.nave == c_evoked.nave
    assert_array_almost_equal(evoked_data, c_evoked_data, 10)
    assert_array_almost_equal(evoked.times, c_evoked.times, 12)


def test_crop(tmp_path):
    """Test of crop of epochs."""
    temp_fname = tmp_path / "test-epo.fif"

    raw, events, picks = _get_data()
    epochs = Epochs(
        raw,
        events[:5],
        event_id,
        tmin,
        tmax,
        picks=picks,
        preload=False,
        reject=reject,
        flat=flat,
    )
    pytest.raises(RuntimeError, epochs.crop, None, 0.2)  # not preloaded
    data_normal = epochs.get_data()

    epochs2 = Epochs(
        raw,
        events[:5],
        event_id,
        tmin,
        tmax,
        picks=picks,
        preload=True,
        reject=reject,
        flat=flat,
    )
    with _record_warnings(), pytest.warns(RuntimeWarning, match="tmax is set to"):
        epochs2.crop(-20, 200)

    # indices for slicing
    tmin_window = tmin + 0.1
    tmax_window = tmax - 0.1
    tmask = (epochs.times >= tmin_window) & (epochs.times <= tmax_window)
    assert tmin_window > tmin
    assert tmax_window < tmax

    epochs3 = epochs2.copy().crop(tmin_window, tmax_window)
    assert epochs3.baseline == epochs2.baseline
    data3 = epochs3.get_data()

    epochs2.crop(tmin_window, tmax_window)
    data2 = epochs2.get_data()

    assert_array_equal(data2, data_normal[:, :, tmask])
    assert_array_equal(data3, data_normal[:, :, tmask])
    assert_array_equal(
        epochs.time_as_index([tmin, tmax], use_rounding=True),
        [0, len(epochs.times) - 1],
    )
    assert_array_equal(
        epochs3.time_as_index([tmin_window, tmax_window], use_rounding=True),
        [0, len(epochs3.times) - 1],
    )

    # test time info is correct
    epochs = EpochsArray(
        np.zeros((1, 1, 1000)),
        create_info(1, 1000.0, "eeg"),
        np.ones((1, 3), int),
        tmin=-0.2,
    )
    epochs.crop(-0.200, 0.700)
    last_time = epochs.times[-1]
    with pytest.warns(RuntimeWarning, match="aliasing"):
        epochs.decimate(10)
    assert_allclose(last_time, epochs.times[-1])
    want_time = epochs.times[-1] - 1.0 / epochs.info["sfreq"]
    epochs.crop(None, epochs.times[-1], include_tmax=False)
    assert_allclose(epochs.times[-1], want_time)

    epochs = Epochs(
        raw,
        events[:5],
        event_id,
        -1,
        1,
        picks=picks,
        preload=True,
        reject=None,
        flat=flat,
    )
    # We include nearest sample, so actually a bit beyond our bounds here
    assert_allclose(epochs.tmin, -1.0006410259015925, rtol=1e-12)
    assert_allclose(epochs.tmax, 1.0006410259015925, rtol=1e-12)
    epochs_crop = epochs.copy().crop(-1, 1)
    assert_allclose(epochs.times, epochs_crop.times, rtol=1e-12)
    # Ensure we don't allow silly crops
    with pytest.warns(RuntimeWarning, match="is set to"):
        pytest.raises(ValueError, epochs.crop, 1000, 2000)
        pytest.raises(ValueError, epochs.crop, 0.1, 0)

    # Test that cropping adjusts reject_tmin and reject_tmax if need be.
    epochs = Epochs(
        raw=raw,
        events=events[:5],
        event_id=event_id,
        tmin=tmin,
        tmax=tmax,
        reject_tmin=tmin,
        reject_tmax=tmax,
    )
    epochs.load_data()
    epochs_cropped = epochs.copy().crop(0, None)
    assert np.isclose(epochs_cropped.tmin, epochs_cropped.reject_tmin)

    epochs_cropped = epochs.copy().crop(None, 0.1)
    assert np.isclose(epochs_cropped.tmax, epochs_cropped.reject_tmax)
    del epochs_cropped

    # Test that repeated cropping is idempotent
    epoch_crop = epochs.copy()
    epoch_crop.crop(None, 0.4, include_tmax=False)
    n_times = len(epoch_crop.times)
    with pytest.warns(RuntimeWarning, match="tmax is set to"):
        epoch_crop.crop(None, 0.4, include_tmax=False)
        assert len(epoch_crop.times) == n_times

    # Cropping & I/O roundtrip
    epochs.crop(0, 0.1)
    epochs.save(temp_fname)
    epochs_read = mne.read_epochs(temp_fname)
    assert np.isclose(epochs_read.tmin, epochs_read.reject_tmin)
    assert np.isclose(epochs_read.tmax, epochs_read.reject_tmax)


def test_resample():
    """Test of resample of epochs."""
    raw, events, picks = _get_data()
    epochs = Epochs(
        raw,
        events[:10],
        event_id,
        tmin,
        tmax,
        picks=picks,
        preload=False,
        reject=reject,
        flat=flat,
    )
    pytest.raises(RuntimeError, epochs.resample, 100)

    epochs_o = Epochs(
        raw,
        events[:10],
        event_id,
        tmin,
        tmax,
        picks=picks,
        preload=True,
        reject=reject,
        flat=flat,
    )
    epochs = epochs_o.copy()

    data_normal = deepcopy(epochs.get_data())
    times_normal = deepcopy(epochs.times)
    sfreq_normal = epochs.info["sfreq"]
    # upsample by 2
    epochs = epochs_o.copy()
    epochs.resample(sfreq_normal * 2, npad=0)
    data_up = deepcopy(epochs.get_data())
    times_up = deepcopy(epochs.times)
    sfreq_up = epochs.info["sfreq"]
    # downsamply by 2, which should match
    epochs.resample(sfreq_normal, npad=0)
    data_new = deepcopy(epochs.get_data())
    times_new = deepcopy(epochs.times)
    sfreq_new = epochs.info["sfreq"]
    assert data_up.shape[2] == 2 * data_normal.shape[2]
    assert sfreq_up == 2 * sfreq_normal
    assert sfreq_new == sfreq_normal
    assert len(times_up) == 2 * len(times_normal)
    assert_array_almost_equal(times_new, times_normal, 10)
    assert data_up.shape[2] == 2 * data_normal.shape[2]
    assert_array_almost_equal(data_new, data_normal, 5)

    # use parallel
    epochs = epochs_o.copy()
    epochs.resample(sfreq_normal * 2, n_jobs=None, npad=0)
    assert np.allclose(data_up, epochs._data, rtol=1e-8, atol=1e-16)

    # test copy flag
    epochs = epochs_o.copy()
    epochs_resampled = epochs.copy().resample(sfreq_normal * 2, npad=0)
    assert epochs_resampled is not epochs
    epochs_resampled = epochs.resample(sfreq_normal * 2, npad=0)
    assert epochs_resampled is epochs

    # test proper setting of times (#2645)
    n_trial, n_chan, n_time, sfreq = 1, 1, 10, 1000.0
    data = np.zeros((n_trial, n_chan, n_time))
    events = np.zeros((n_trial, 3), int)
    info = create_info(n_chan, sfreq, "eeg")
    epochs1 = EpochsArray(data, deepcopy(info), events)
    epochs2 = EpochsArray(data, deepcopy(info), events)
    epochs = concatenate_epochs([epochs1, epochs2])
    epochs1.resample(epochs1.info["sfreq"] // 2, npad="auto")
    epochs2.resample(epochs2.info["sfreq"] // 2, npad="auto")
    epochs = concatenate_epochs([epochs1, epochs2])
    for e in epochs1, epochs2, epochs:
        assert_equal(e.times[0], epochs.tmin)
        assert_equal(e.times[-1], epochs.tmax)
    # test that cropping after resampling works (#3296)
    this_tmin = -0.002
    epochs = EpochsArray(data, deepcopy(info), events, tmin=this_tmin)
    for times in (epochs.times, epochs._raw_times):
        assert_allclose(times, np.arange(n_time) / sfreq + this_tmin)
    epochs.resample(info["sfreq"] * 2.0)
    for times in (epochs.times, epochs._raw_times):
        assert_allclose(times, np.arange(2 * n_time) / (sfreq * 2) + this_tmin)
    epochs.crop(0, None)
    for times in (epochs.times, epochs._raw_times):
        assert_allclose(times, np.arange((n_time - 2) * 2) / (sfreq * 2))
    epochs.resample(sfreq)
    for times in (epochs.times, epochs._raw_times):
        assert_allclose(times, np.arange(n_time - 2) / sfreq)


def test_detrend():
    """Test detrending of epochs."""
    raw, events, picks = _get_data()

    # test first-order
    epochs_1 = Epochs(
        raw, events[:4], event_id, tmin, tmax, picks=picks, baseline=None, detrend=1
    )
    epochs_2 = Epochs(
        raw, events[:4], event_id, tmin, tmax, picks=picks, baseline=None, detrend=None
    )
    data_picks = pick_types(epochs_1.info, meg=True, eeg=True, exclude="bads")
    evoked_1 = epochs_1.average()
    evoked_2 = epochs_2.average()
    evoked_2.detrend(1)
    # Due to roundoff these won't be exactly equal, but they should be close
    assert_allclose(evoked_1.data, evoked_2.data, rtol=1e-8, atol=1e-20)

    # test zeroth-order case
    for preload in [True, False]:
        epochs_1 = Epochs(
            raw,
            events[:4],
            event_id,
            tmin,
            tmax,
            picks=picks,
            baseline=(None, None),
            preload=preload,
        )
        epochs_2 = Epochs(
            raw,
            events[:4],
            event_id,
            tmin,
            tmax,
            picks=picks,
            baseline=None,
            preload=preload,
            detrend=0,
        )
        a = epochs_1.get_data()
        b = epochs_2.get_data()
        # All data channels should be almost equal
        assert_allclose(
            a[:, data_picks, :], b[:, data_picks, :], rtol=1e-16, atol=1e-20
        )
        # There are non-M/EEG channels that should not be equal:
        assert not np.allclose(a, b)

    for value in ["foo", 2, False, True]:
        pytest.raises(
            ValueError, Epochs, raw, events[:4], event_id, tmin, tmax, detrend=value
        )


def test_bootstrap():
    """Test of bootstrapping of epochs."""
    raw, events, picks = _get_data()
    epochs = Epochs(
        raw,
        events[:5],
        event_id,
        tmin,
        tmax,
        picks=picks,
        preload=True,
        reject=reject,
        flat=flat,
    )
    random_states = [0, np.random.default_rng(0)]
    for random_state in random_states:
        epochs2 = bootstrap(epochs, random_state=random_state)
        assert len(epochs2.events) == len(epochs.events)
        assert epochs._data.shape == epochs2._data.shape


def test_epochs_copy():
    """Test copy epochs."""
    raw, events, picks = _get_data()
    epochs = Epochs(
        raw,
        events[:5],
        event_id,
        tmin,
        tmax,
        picks=picks,
        preload=True,
        reject=reject,
        flat=flat,
    )
    copied = epochs.copy()
    assert_array_equal(epochs._data, copied._data)

    epochs = Epochs(
        raw,
        events[:5],
        event_id,
        tmin,
        tmax,
        picks=picks,
        preload=False,
        reject=reject,
        flat=flat,
    )
    copied = epochs.copy()
    data = epochs.get_data()
    copied_data = copied.get_data()
    assert_array_equal(data, copied_data)


def test_iter_evoked():
    """Test the iterator for epochs -> evoked."""
    raw, events, picks = _get_data()
    epochs = Epochs(raw, events[:5], event_id, tmin, tmax, picks=picks)

    for ii, ev in enumerate(epochs.iter_evoked()):
        x = ev.data
        y = epochs.get_data()[ii, :, :]
        assert_array_equal(x, y)


@pytest.mark.parametrize("preload", (True, False))
def test_iter_epochs(preload):
    """Test iteration over epochs."""
    raw, events, picks = _get_data()
    epochs = Epochs(raw, events[:5], event_id, tmin, tmax, picks=picks, preload=preload)
    assert not hasattr(epochs, "_current_detrend_picks")
    epochs_data = epochs.get_data()
    data = list()
    for _ in range(10):
        try:
            data.append(next(epochs))
        except StopIteration:
            break
        else:
            assert hasattr(epochs, "_current_detrend_picks")
    assert not hasattr(epochs, "_current_detrend_picks")
    data = np.array(data)
    assert_allclose(data, epochs_data, atol=1e-20)


def test_subtract_evoked():
    """Test subtraction of Evoked from Epochs."""
    raw, events, picks = _get_data()
    epochs = Epochs(raw, events[:10], event_id, tmin, tmax, picks=picks)

    # make sure subtraction fails if data channels are missing
    pytest.raises(ValueError, epochs.subtract_evoked, epochs.average(picks[:5]))

    # do the subtraction using the default argument
    epochs.subtract_evoked()

    # apply SSP now
    epochs.apply_proj()

    # use preloading and SSP from the start
    epochs2 = Epochs(raw, events[:10], event_id, tmin, tmax, picks=picks, preload=True)

    evoked = epochs2.average()
    epochs2.subtract_evoked(evoked)

    # this gives the same result
    assert_allclose(epochs.get_data(), epochs2.get_data())

    # if we compute the evoked response after subtracting it we get zero
    zero_evoked = epochs.average()
    data = zero_evoked.data
    assert_allclose(data, np.zeros_like(data), atol=1e-15)

    # with decimation (gh-7854)
    epochs3 = Epochs(
        raw, events[:10], event_id, tmin, tmax, picks=picks, decim=10, verbose="error"
    )
    data_old = epochs2.decimate(10, verbose="error").get_data()
    data = epochs3.subtract_evoked().get_data()
    assert_allclose(data, data_old)
    assert_allclose(epochs3.average().data, 0.0, atol=1e-20)


def test_epoch_eq():
    """Test for equalize_epoch_counts and equalize_event_counts functions."""
    # load data
    raw, events, picks = _get_data()
    # test equalize epoch counts
    # create epochs with unequal counts
    events_1 = events[events[:, 2] == event_id]
    epochs_1 = Epochs(raw, events_1, event_id, tmin, tmax, picks=picks)
    events_2 = events[events[:, 2] == event_id_2]
    epochs_2 = Epochs(raw, events_2, event_id_2, tmin, tmax, picks=picks)
    # events 2 has one more event than events 1
    epochs_1.drop_bad()  # make sure drops are logged
    epochs_2.drop_bad()  # make sure drops are logged
    # make sure there is a difference in the number of events
    assert len(epochs_1) != len(epochs_2)
    # make sure bad epochs are dropped before equalizing epoch counts
    assert_equal(
        len([log for log in epochs_1.drop_log if not log]), len(epochs_1.events)
    )
    assert epochs_2.drop_log == ((),) * len(epochs_2.events)
    # test mintime method
    events_1[-1, 0] += 60  # hack: ensure mintime drops something other than last trial
    # now run equalize_epoch_counts with mintime method
    equalize_epoch_counts([epochs_1, epochs_2], method="mintime")
    # mintime method should give us the smallest difference between timings of epochs
    alleged_mintime = np.sum(np.abs(epochs_1.events[:, 0] - epochs_2.events[:, 0]))
    # test that "mintime" works as expected, by systematically dropping each event from
    # events_2 and ensuring the latencies are actually smallest in the
    # equalize_epoch_counts case. NB: len(events_2) > len(events_1)
    for idx in range(events_2.shape[0]):
        # delete epoch from events_2
        test_events = np.delete(events_2.copy(), idx, axis=0)
        assert test_events.shape == epochs_1.events.shape == epochs_2.events.shape
        # difference (in samples) between epochs_1 event times and the event times we
        # get from our deletion of row `idx` from events_2
        latencies = epochs_1.events[:, 0] - test_events[:, 0]
        got_mintime = np.sum(np.abs(latencies))
        assert got_mintime >= alleged_mintime
    # make sure the number of events is equal
    assert_equal(epochs_1.events.shape[0], epochs_2.events.shape[0])
    # create new epochs with the same event ids as epochs_1 and epochs_2
    epochs_3 = Epochs(raw, events, event_id, tmin, tmax, picks=picks)
    epochs_4 = Epochs(raw, events, event_id_2, tmin, tmax, picks=picks)
    epochs_3.drop_bad()  # make sure drops are logged
    epochs_4.drop_bad()  # make sure drops are logged
    # make sure there is a difference in the number of events
    assert len(epochs_3) != len(epochs_4)
    # test truncate method
    equalize_epoch_counts([epochs_3, epochs_4], method="truncate")
    if len(epochs_3.events) > len(epochs_4.events):
        assert_equal(epochs_3.events[-2, 0], epochs_3.events.shape[-1, 0])
    elif len(epochs_3.events) < len(epochs_4.events):
        assert_equal(epochs_4.events[-2, 0], epochs_4.events[-1, 0])
    assert_equal(epochs_1.events.shape[0], epochs_3.events.shape[0])
    assert_equal(epochs_3.events.shape[0], epochs_4.events.shape[0])

    # equalizing conditions
    epochs = Epochs(
        raw,
        events,
        {"a": 1, "b": 2, "c": 3, "d": 4},
        tmin,
        tmax,
        picks=picks,
        reject=reject,
    )
    epochs.drop_bad()  # make sure drops are logged
    assert_equal(len([log for log in epochs.drop_log if not log]), len(epochs.events))
    drop_log1 = deepcopy(epochs.drop_log)
    old_shapes = [epochs[key].events.shape[0] for key in ["a", "b", "c", "d"]]
    epochs.equalize_event_counts(["a", "b"])
    # undo the eq logging
    drop_log2 = tuple(
        () if log == ("EQUALIZED_COUNT",) else log for log in epochs.drop_log
    )
    assert_equal(drop_log1, drop_log2)

    assert_equal(len([log for log in epochs.drop_log if not log]), len(epochs.events))
    new_shapes = [epochs[key].events.shape[0] for key in ["a", "b", "c", "d"]]
    assert_equal(new_shapes[0], new_shapes[1])
    assert_equal(new_shapes[2], new_shapes[2])
    assert_equal(new_shapes[3], new_shapes[3])
    # now with two conditions collapsed
    old_shapes = new_shapes
    epochs.equalize_event_counts([["a", "b"], "c"])
    new_shapes = [epochs[key].events.shape[0] for key in ["a", "b", "c", "d"]]
    assert_equal(new_shapes[0] + new_shapes[1], new_shapes[2])
    assert_equal(new_shapes[3], old_shapes[3])
    with pytest.raises(ValueError, match="keys must be strings, got"):
        epochs.equalize_event_counts([1, "a"])

    # now let's combine conditions
    old_shapes = new_shapes
    epochs.equalize_event_counts([["a", "b"], ["c", "d"]])
    new_shapes = [epochs[key].events.shape[0] for key in ["a", "b", "c", "d"]]
    assert_equal(old_shapes[0] + old_shapes[1], new_shapes[0] + new_shapes[1])
    assert_equal(new_shapes[0] + new_shapes[1], new_shapes[2] + new_shapes[3])
    with pytest.raises(ValueError, match="value must not already exist"):
        combine_event_ids(epochs, ["a", "b"], {"ab": 1})

    combine_event_ids(epochs, ["a", "b"], {"ab": np.int32(12)}, copy=False)
    caught = 0
    for key in ["a", "b"]:
        try:
            epochs[key]
        except KeyError:
            caught += 1
    assert_equal(caught, 2)
    assert not np.any(epochs.events[:, 2] == 1)
    assert not np.any(epochs.events[:, 2] == 2)
    epochs = combine_event_ids(epochs, ["c", "d"], {"cd": 34})
    assert np.all(np.logical_or(epochs.events[:, 2] == 12, epochs.events[:, 2] == 34))
    assert_equal(epochs["ab"].events.shape[0], old_shapes[0] + old_shapes[1])
    assert_equal(epochs["ab"].events.shape[0], epochs["cd"].events.shape[0])

    # equalizing with hierarchical tags
    epochs = Epochs(
        raw,
        events,
        {"a/x": 1, "b/x": 2, "a/y": 3, "b/y": 4},
        tmin,
        tmax,
        picks=picks,
        reject=reject,
    )
    cond1, cond2 = ["a", ["b/x", "b/y"]], [["a/x", "a/y"], "b"]
    es = [epochs.copy().equalize_event_counts(c)[0] for c in (cond1, cond2)]
    assert_array_equal(es[0].events[:, 0], es[1].events[:, 0])
    with pytest.raises(ValueError, match="mix hierarchical and regular"):
        epochs.equalize_event_counts(["a", ["b", "b/y"]])
    with pytest.raises(ValueError, match="overlapping. Provide an orthogonal"):
        epochs.equalize_event_counts([["a/x", "a/y"], "x"])
    with pytest.raises(KeyError, match="not found in the epoch object"):
        epochs.equalize_event_counts(["a/no_match", "b"])
    # test equalization with only one epoch in each cond
    epo = epochs[[0, 1, 5]]
    assert len(epo["x"]) == 2
    assert len(epo["y"]) == 1
    epo_, drop_inds = epo.equalize_event_counts()
    assert len(epo_) == 2
    assert drop_inds.shape == (1,)
    # test equalization with no events of one type
    epochs.drop(np.arange(10))
    assert_equal(len(epochs["a/x"]), 0)
    assert len(epochs["a/y"]) > 0
    epochs.equalize_event_counts(["a/x", "a/y"])
    assert_equal(len(epochs["a/x"]), 0)
    assert_equal(len(epochs["a/y"]), 0)

    # test default behavior (event_ids=None)
    epochs = Epochs(
        raw,
        events,
        {"a": 1, "b": 2, "c": 3, "d": 4},
        tmin,
        tmax,
        picks=picks,
        reject=reject,
    )
    epochs_1, _ = epochs.copy().equalize_event_counts()
    epochs_2, _ = epochs.copy().equalize_event_counts(list(epochs.event_id))
    assert_array_equal(epochs_1.events, epochs_2.events)

    # test invalid values of event_ids
    with pytest.raises(TypeError, match="received a string"):
        epochs.equalize_event_counts("hello!")

    with pytest.raises(TypeError, match="list-like or None"):
        epochs.equalize_event_counts(1.5)


def test_equalize_epoch_counts_random():
    """Test random equalization of epochs."""
    raw, events, picks = _get_data()
    # create epochs with unequal counts
    events_1 = events[events[:, 2] == event_id]
    epochs_1 = Epochs(raw, events_1, event_id, tmin, tmax, picks=picks)
    events_2 = events[events[:, 2] == event_id_2]
    epochs_2 = Epochs(raw, events_2, event_id_2, tmin, tmax, picks=picks)
    epochs_1.drop_bad()
    epochs_2.drop_bad()
    assert len(epochs_1) != len(epochs_2)
    equalize_epoch_counts([epochs_1, epochs_2], method="random")
    assert len(epochs_1) == len(epochs_2)


def test_access_by_name(tmp_path):
    """Test accessing epochs by event name and on_missing for rare events."""
    raw, events, picks = _get_data()

    # Test various invalid inputs
    pytest.raises(
        TypeError, Epochs, raw, events, {1: 42, 2: 42}, tmin, tmax, picks=picks
    )
    pytest.raises(
        TypeError,
        Epochs,
        raw,
        events,
        {"a": "spam", 2: "eggs"},
        tmin,
        tmax,
        picks=picks,
    )
    pytest.raises(
        TypeError,
        Epochs,
        raw,
        events,
        {"a": "spam", 2: "eggs"},
        tmin,
        tmax,
        picks=picks,
    )
    pytest.raises(TypeError, Epochs, raw, events, "foo", tmin, tmax, picks=picks)
    pytest.raises(TypeError, Epochs, raw, events, ["foo"], tmin, tmax, picks=picks)

    # Test accessing non-existent events (assumes 12345678 does not exist)
    event_id_illegal = dict(aud_l=1, does_not_exist=12345678)
    pytest.raises(ValueError, Epochs, raw, events, event_id_illegal, tmin, tmax)
    # Test on_missing
    pytest.raises(
        ValueError, Epochs, raw, events, event_id_illegal, tmin, tmax, on_missing="foo"
    )
    with pytest.warns(RuntimeWarning, match="No matching events"):
        Epochs(raw, events, event_id_illegal, tmin, tmax, on_missing="warn")
    Epochs(raw, events, event_id_illegal, tmin, tmax, on_missing="ignore")

    # Test constructing epochs with a list of ints as events
    epochs = Epochs(raw, events, [1, 2], tmin, tmax, picks=picks)
    for k, v in epochs.event_id.items():
        assert_equal(int(k), v)

    epochs = Epochs(raw, events, {"a": 1, "b": 2}, tmin, tmax, picks=picks)
    pytest.raises(KeyError, epochs.__getitem__, "bar")

    data = epochs["a"].get_data()
    event_a = events[events[:, 2] == 1]
    assert len(data) == len(event_a)

    epochs = Epochs(
        raw, events, {"a": 1, "b": 2}, tmin, tmax, picks=picks, preload=True
    )
    pytest.raises(KeyError, epochs.__getitem__, "bar")
    temp_fname = tmp_path / "test-epo.fif"
    epochs.save(temp_fname, overwrite=True)
    epochs2 = read_epochs(temp_fname)

    for ep in [epochs, epochs2]:
        data = ep["a"].get_data()
        event_a = events[events[:, 2] == 1]
        assert len(data) == len(event_a)

    assert_array_equal(epochs2["a"].events, epochs["a"].events)

    epochs3 = Epochs(
        raw,
        events,
        {"a": 1, "b": 2, "c": 3, "d": 4},
        tmin,
        tmax,
        picks=picks,
        preload=True,
    )
    assert_equal(list(sorted(epochs3[("a", "b")].event_id.values())), [1, 2])
    epochs4 = epochs["a"]
    epochs5 = epochs3["a"]
    assert_array_equal(epochs4.events, epochs5.events)
    # 20 is our tolerance because epochs are written out as floats
    assert_array_almost_equal(epochs4.get_data(), epochs5.get_data(), 20)
    epochs6 = epochs3[["a", "b"]]
    assert all(np.logical_or(epochs6.events[:, 2] == 1, epochs6.events[:, 2] == 2))
    assert_array_equal(epochs.events, epochs6.events)
    assert_array_almost_equal(epochs.get_data(), epochs6.get_data(), 20)

    # Make sure we preserve names
    assert_equal(epochs["a"]._name, "a")
    assert_equal(epochs[["a", "b"]]["a"]._name, "a")


@pytest.mark.slowtest
def test_to_data_frame():
    """Test epochs Pandas exporter."""
    pytest.importorskip("pandas")
    raw, events, picks = _get_data()
    epochs = Epochs(raw, events, {"a": 1, "b": 2}, tmin, tmax, picks=picks)
    # test index checking
    with pytest.raises(ValueError, match="options. Valid index options are"):
        epochs.to_data_frame(index=["foo", "bar"])
    with pytest.raises(ValueError, match='"qux" is not a valid option'):
        epochs.to_data_frame(index="qux")
    with pytest.raises(TypeError, match="index must be `None` or a string or"):
        epochs.to_data_frame(index=np.arange(400))
    # test wide format
    df_wide = epochs.to_data_frame()
    assert all(np.isin(epochs.ch_names, df_wide.columns))
    assert all(np.isin(["time", "epoch", "condition"], df_wide.columns))
    # test long format
    df_long = epochs.to_data_frame(long_format=True)
    expected = ("condition", "epoch", "time", "channel", "ch_type", "value")
    assert set(expected) == set(df_long.columns)
    assert set(epochs.ch_names) == set(df_long["channel"])
    assert len(df_long) == epochs.get_data().size
    # test long format w/ index
    df_long = epochs.to_data_frame(long_format=True, index=["epoch"])
    del df_wide, df_long
    # test scalings
    df = epochs.to_data_frame(index=["condition", "epoch", "time"])
    data = np.hstack(epochs.get_data())
    assert_array_equal(df.values[:, 0], data[0] * 1e13)
    assert_array_equal(df.values[:, 2], data[2] * 1e15)


@pytest.mark.parametrize(
    "index",
    (
        "time",
        ["condition", "time", "epoch"],
        ["epoch", "time"],
        ["time", "epoch"],
        None,
    ),
)
def test_to_data_frame_index(index):
    """Test index creation in epochs Pandas exporter."""
    pytest.importorskip("pandas")
    raw, events, picks = _get_data()
    epochs = Epochs(raw, events, {"a": 1, "b": 2}, tmin, tmax, picks=picks)
    df = epochs.to_data_frame(picks=[11, 12, 14], index=index)
    # test index order/hierarchy preservation
    if not isinstance(index, list):
        index = [index]
    assert list(df.index.names) == index
    # test that non-indexed data were present as columns
    non_index = list(set(["condition", "time", "epoch"]) - set(index))
    if len(non_index):
        assert all(np.isin(non_index, df.columns))


@pytest.mark.parametrize("time_format", (None, "ms", "timedelta"))
def test_to_data_frame_time_format(time_format):
    """Test time conversion in epochs Pandas exporter."""
    pd = pytest.importorskip("pandas")
    raw, events, picks = _get_data()
    epochs = Epochs(raw, events, {"a": 1, "b": 2}, tmin, tmax, picks=picks)
    # test time_format
    df = epochs.to_data_frame(time_format=time_format)
    dtypes = {None: np.float64, "ms": np.int64, "timedelta": pd.Timedelta}
    assert isinstance(df["time"].iloc[0], dtypes[time_format])


def test_epochs_proj_mixin():
    """Test SSP proj methods from ProjMixin class."""
    raw, events, picks = _get_data()
    for proj in [True, False]:
        epochs = Epochs(raw, events[:4], event_id, tmin, tmax, picks=picks, proj=proj)

        assert all(p["active"] == proj for p in epochs.info["projs"])

        # test adding / deleting proj
        if proj:
            epochs.get_data()
            assert all(p["active"] == proj for p in epochs.info["projs"])
            pytest.raises(
                ValueError,
                epochs.add_proj,
                epochs.info["projs"][0],
                {"remove_existing": True},
            )
            pytest.raises(ValueError, epochs.add_proj, "spam")
            pytest.raises(ValueError, epochs.del_proj, 0)
        else:
            projs = deepcopy(epochs.info["projs"])
            n_proj = len(epochs.info["projs"])
            epochs.del_proj(0)
            assert len(epochs.info["projs"]) == n_proj - 1
            # Test that already existing projections are not added.
            epochs.add_proj(projs, remove_existing=False)
            assert len(epochs.info["projs"]) == n_proj
            epochs.add_proj(projs[:-1], remove_existing=True)
            assert len(epochs.info["projs"]) == n_proj - 1

    # catch no-gos.
    # wrong proj argument
    pytest.raises(
        ValueError,
        Epochs,
        raw,
        events[:4],
        event_id,
        tmin,
        tmax,
        picks=picks,
        proj="crazy",
    )

    for preload in [True, False]:
        epochs = Epochs(
            raw,
            events[:4],
            event_id,
            tmin,
            tmax,
            picks=picks,
            proj="delayed",
            preload=preload,
            reject=reject,
        ).set_eeg_reference(projection=True)
        epochs_proj = (
            Epochs(
                raw,
                events[:4],
                event_id,
                tmin,
                tmax,
                picks=picks,
                proj=True,
                preload=preload,
                reject=reject,
            )
            .set_eeg_reference(projection=True)
            .apply_proj()
        )

        epochs_noproj = Epochs(
            raw,
            events[:4],
            event_id,
            tmin,
            tmax,
            picks=picks,
            proj=False,
            preload=preload,
            reject=reject,
        )
        epochs_noproj.set_eeg_reference(projection=True)

        assert_allclose(
            epochs.copy().apply_proj().get_data(),
            epochs_proj.get_data(),
            rtol=1e-10,
            atol=1e-25,
        )
        assert_allclose(
            epochs.get_data(), epochs_noproj.get_data(), rtol=1e-10, atol=1e-25
        )

        # make sure data output is constant across repeated calls
        # e.g. drop bads
        assert_array_equal(epochs.get_data(), epochs.get_data())
        assert_array_equal(epochs_proj.get_data(), epochs_proj.get_data())
        assert_array_equal(epochs_noproj.get_data(), epochs_noproj.get_data())

    # test epochs.next calls
    data = epochs.get_data().copy()
    data2 = np.array([e for e in epochs])
    assert_array_equal(data, data2)

    # cross application from processing stream 1 to 2
    epochs.apply_proj()
    assert_array_equal(epochs._projector, epochs_proj._projector)
    assert_allclose(epochs._data, epochs_proj.get_data())

    # test mixin against manual application
    epochs = Epochs(
        raw, events[:4], event_id, tmin, tmax, picks=picks, baseline=None, proj=False
    ).set_eeg_reference(projection=True)
    data = epochs.get_data().copy()
    epochs.apply_proj()
    assert_allclose(np.dot(epochs._projector, data[0]), epochs._data[0])


def test_delayed_epochs():
    """Test delayed projection on Epochs."""
    raw, events, picks = _get_data()
    events = events[:10]
    picks = np.concatenate(
        [
            pick_types(raw.info, meg=True, eeg=True)[::22],
            pick_types(raw.info, meg=False, eeg=False, ecg=True, eog=True),
        ]
    )
    picks = np.sort(picks)
    raw.load_data().pick([raw.ch_names[pick] for pick in picks])
    raw.info.normalize_proj()
    del picks
    n_epochs = 2  # number we expect after rejection
    with raw.info._unlock():
        raw.info["lowpass"] = 40.0  # fake the LP info so no warnings
    for decim in (1, 3):
        proj_data = Epochs(
            raw, events, event_id, tmin, tmax, proj=True, reject=reject, decim=decim
        )
        use_tmin = proj_data.tmin
        proj_data = proj_data.get_data()
        noproj_data = Epochs(
            raw, events, event_id, tmin, tmax, proj=False, reject=reject, decim=decim
        ).get_data()
        assert_equal(proj_data.shape, noproj_data.shape)
        assert_equal(proj_data.shape[0], n_epochs)
        for preload in (True, False):
            for proj in (True, False, "delayed"):
                for ii in range(3):
                    print(decim, preload, proj, ii)
                    comp = proj_data if proj is True else noproj_data
                    if ii in (0, 1):
                        epochs = Epochs(
                            raw,
                            events,
                            event_id,
                            tmin,
                            tmax,
                            proj=proj,
                            reject=reject,
                            preload=preload,
                            decim=decim,
                        )
                    else:
                        fake_events = np.zeros((len(comp), 3), int)
                        fake_events[:, 0] = np.arange(len(comp))
                        fake_events[:, 2] = 1
                        epochs = EpochsArray(
                            comp,
                            raw.info,
                            tmin=use_tmin,
                            event_id=1,
                            events=fake_events,
                            proj=proj,
                        )
                        with epochs.info._unlock():
                            epochs.info["sfreq"] /= decim
                        assert_equal(len(epochs), n_epochs)
                    assert raw.proj is False
                    assert epochs.proj is (True if proj is True else False)
                    if ii == 1:
                        epochs.load_data()
                    picks_data = pick_types(epochs.info, meg=True, eeg=True)
                    evoked = epochs.average(picks=picks_data)
                    assert_equal(evoked.nave, n_epochs, str(epochs.drop_log))
                    if proj is True:
                        evoked.apply_proj()
                    else:
                        assert evoked.proj is False
                    assert_array_equal(
                        evoked.ch_names, np.array(epochs.ch_names)[picks_data]
                    )
                    assert_allclose(evoked.times, epochs.times)
                    epochs_data = epochs.get_data()
                    assert_allclose(
                        evoked.data,
                        epochs_data.mean(axis=0)[picks_data],
                        rtol=1e-5,
                        atol=1e-20,
                    )
                    assert_allclose(epochs_data, comp, rtol=1e-5, atol=1e-20)


def test_drop_epochs():
    """Test dropping of epochs."""
    raw, events, picks = _get_data()
    epochs = Epochs(raw, events, event_id, tmin, tmax, picks=picks)
    events1 = events[events[:, 2] == event_id]

    # Bound checks
    with pytest.raises(IndexError, match=r"Epoch index .* is out of bounds"):
        epochs.drop([len(epochs.events)])
    with pytest.raises(IndexError, match=r"Epoch index .* is out of bounds"):
        epochs.drop([-len(epochs.events) - 1])
    with pytest.raises(TypeError, match="indices must be a scalar or a 1-d array"):
        epochs.drop([[1, 2], [3, 4]])
    with pytest.raises(
        TypeError, match=r".* must be an instance of .* got <class '.*'> instead."
    ):
        epochs.drop([1], reason=("a", "b", 2))

    # Test selection attribute
    assert_array_equal(epochs.selection, np.where(events[:, 2] == event_id)[0])
    assert_equal(len(epochs.drop_log), len(events))
    assert all(
        epochs.drop_log[k] == ("IGNORED",)
        for k in set(range(len(events))) - set(epochs.selection)
    )

    selection = epochs.selection.copy()
    n_events = len(epochs.events)
    epochs.drop([2, 4], reason="d")
    assert_equal(epochs.drop_log_stats(), 2.0 / n_events * 100)
    assert_equal(len(epochs.drop_log), len(events))
    assert_equal([epochs.drop_log[k] for k in selection[[2, 4]]], [["d"], ["d"]])
    assert_array_equal(events[epochs.selection], events1[[0, 1, 3, 5, 6]])
    assert_array_equal(events[epochs[3:].selection], events1[[5, 6]])
    assert_array_equal(events[epochs["1"].selection], events1[[0, 1, 3, 5, 6]])

    # Test using tuple to drop epochs
    raw, events, picks = _get_data()
    epochs_tuple = Epochs(raw, events, event_id, tmin, tmax, picks=picks, preload=True)
    selection_tuple = epochs_tuple.selection.copy()
    epochs_tuple.drop((2, 3, 4), reason=("a", "b"))
    n_events = len(epochs.events)
    assert [epochs_tuple.drop_log[k] for k in selection_tuple[[2, 3, 4]]] == [
        ("a", "b"),
        ("a", "b"),
        ("a", "b"),
    ]


@pytest.mark.parametrize("preload", (True, False))
def test_drop_epochs_mult(preload):
    """Test that subselecting epochs or making fewer epochs is similar."""
    raw, events, picks = _get_data()
    assert_array_equal(events[14], [33712, 0, 1])  # event type a
    epochs1 = Epochs(
        raw,
        events,
        {"a": 1, "b": 2},
        tmin,
        tmax,
        picks=picks,
        reject=reject,
        preload=preload,
    )
    epochs2 = Epochs(
        raw, events, {"a": 1}, tmin, tmax, picks=picks, reject=reject, preload=preload
    )
    epochs1 = epochs1["a"]
    assert_array_equal(epochs1.events, epochs2.events)
    assert_array_equal(epochs1.selection, epochs2.selection)

    if preload:
        # In the preload case you cannot know the bads if already ignored
        assert len(epochs1.drop_log) == len(epochs2.drop_log)
        for di, (d1, d2) in enumerate(zip(epochs1.drop_log, epochs2.drop_log)):
            assert isinstance(d1, tuple)
            assert isinstance(d2, tuple)
            msg = f"\nepochs1.drop_log[{di}] = {d1}, \nepochs2.drop_log[{di}] = {d2}"
            if "IGNORED" in d1:
                assert "IGNORED" in d2, msg
            if "IGNORED" not in d1 and d1 != ():
                assert (d2 == d1) or (d2 == ("IGNORED",)), msg
            if d1 == ():
                assert d2 == (), msg
    else:
        # In the non preload is should be exactly the same
        assert epochs1.drop_log == epochs2.drop_log


def test_contains():
    """Test membership API."""
    raw, events = _get_data(True)[:2]
    # Add seeg channel
    seeg = RawArray(
        np.zeros((1, len(raw.times))),
        create_info(["SEEG 001"], raw.info["sfreq"], "seeg"),
    )
    with seeg.info._unlock():
        for key in (
            "dev_head_t",
            "highpass",
            "lowpass",
            "dig",
            "description",
            "acq_pars",
            "experimenter",
            "proj_name",
        ):
            seeg.info[key] = raw.info[key]
    raw.add_channels([seeg])
    # Add dbs channel
    dbs = RawArray(
        np.zeros((1, len(raw.times))),
        create_info(["DBS 001"], raw.info["sfreq"], "dbs"),
    )
    with dbs.info._unlock():
        for key in (
            "dev_head_t",
            "highpass",
            "lowpass",
            "dig",
            "description",
            "acq_pars",
            "experimenter",
            "proj_name",
        ):
            dbs.info[key] = raw.info[key]
    raw.add_channels([dbs])
    tests = [
        (("mag", False, False, False), ("grad", "eeg", "seeg", "dbs")),
        (("grad", False, False, False), ("mag", "eeg", "seeg", "dbs")),
        ((False, True, False, False), ("grad", "mag", "seeg", "dbs")),
        ((False, False, True, False), ("grad", "mag", "eeg", "dbs")),
    ]

    for (meg, eeg, seeg, dbs), others in tests:
        picks_contains = pick_types(raw.info, meg=meg, eeg=eeg, seeg=seeg, dbs=dbs)
        epochs = Epochs(raw, events, {"a": 1, "b": 2}, tmin, tmax, picks=picks_contains)
        if eeg:
            test = "eeg"
        elif seeg:
            test = "seeg"
        elif dbs:
            test = "dbs"
        else:
            test = meg
        assert test in epochs
        assert not any(o in epochs for o in others)

    pytest.raises(ValueError, epochs.__contains__, "foo")
    pytest.raises(TypeError, epochs.__contains__, 1)


def test_drop_channels_mixin():
    """Test channels-dropping functionality."""
    raw, events = _get_data()[:2]
    # here without picks to get additional coverage
    epochs = Epochs(raw, events, event_id, tmin, tmax, preload=True)
    drop_ch = epochs.ch_names[:3]
    ch_names = epochs.ch_names[3:]

    ch_names_orig = epochs.ch_names
    dummy = epochs.copy().drop_channels(drop_ch)
    assert_equal(ch_names, dummy.ch_names)
    assert_equal(ch_names_orig, epochs.ch_names)
    assert_equal(len(ch_names_orig), epochs.get_data().shape[1])

    epochs.drop_channels(drop_ch)
    assert_equal(ch_names, epochs.ch_names)
    assert_equal(len(ch_names), epochs.get_data().shape[1])


def test_pick_channels_mixin():
    """Test channel-picking functionality."""
    raw, events, picks = _get_data()
    epochs = Epochs(raw, events, event_id, tmin, tmax, picks=picks, preload=True)
    ch_names = epochs.ch_names[:3]
    epochs.preload = False
    pytest.raises(RuntimeError, epochs.drop_channels, [ch_names[0]])
    epochs.preload = True
    ch_names_orig = epochs.ch_names
    dummy = epochs.copy().pick(ch_names)
    assert_equal(ch_names, dummy.ch_names)
    assert_equal(ch_names_orig, epochs.ch_names)
    assert_equal(len(ch_names_orig), epochs.get_data().shape[1])

    epochs.pick(ch_names)
    assert_equal(ch_names, epochs.ch_names)
    assert_equal(len(ch_names), epochs.get_data().shape[1])

    # Invalid picks
    pytest.raises(ValueError, Epochs, raw, events, event_id, tmin, tmax, picks=[])


def test_equalize_channels():
    """Test equalization of channels."""
    raw, events, picks = _get_data()
    epochs1 = Epochs(
        raw, events, event_id, tmin, tmax, picks=picks, proj=False, preload=True
    )
    epochs2 = epochs1.copy()
    ch_names = epochs1.ch_names[2:]
    epochs1.drop_channels(epochs1.ch_names[:1])
    epochs2.drop_channels(epochs2.ch_names[1:2])
    my_comparison = [epochs1, epochs2]
    my_comparison = equalize_channels(my_comparison)
    for e in my_comparison:
        assert_equal(ch_names, e.ch_names)


def test_illegal_event_id():
    """Test handling of invalid events ids."""
    raw, events, picks = _get_data()
    event_id_illegal = dict(aud_l=1, does_not_exist=12345678)

    pytest.raises(
        ValueError,
        Epochs,
        raw,
        events,
        event_id_illegal,
        tmin,
        tmax,
        picks=picks,
        proj=False,
    )


def test_add_channels_epochs():
    """Test adding channels."""
    raw, events, picks = _get_data()

    def make_epochs(picks, proj):
        return Epochs(
            raw, events, event_id, tmin, tmax, preload=True, proj=proj, picks=picks
        )

    picks = pick_types(raw.info, meg=True, eeg=True, exclude="bads")
    picks_meg = pick_types(raw.info, meg=True, eeg=False, exclude="bads")
    picks_eeg = pick_types(raw.info, meg=False, eeg=True, exclude="bads")

    for proj in (False, True):
        epochs = make_epochs(picks=picks, proj=proj)
        epochs_meg = make_epochs(picks=picks_meg, proj=proj)
        assert not epochs_meg.times.flags["WRITEABLE"]
        epochs_eeg = make_epochs(picks=picks_eeg, proj=proj)
        epochs.info._check_consistency()
        epochs_meg.info._check_consistency()
        epochs_eeg.info._check_consistency()

        epochs2 = epochs_meg.copy().add_channels([epochs_eeg])

        assert_equal(len(epochs.info["projs"]), len(epochs2.info["projs"]))
        assert_equal(len(epochs.info.keys()), len(epochs_meg.info.keys()))
        assert_equal(len(epochs.info.keys()), len(epochs_eeg.info.keys()))
        assert_equal(len(epochs.info.keys()), len(epochs2.info.keys()))

        data1 = epochs.get_data()
        data2 = epochs2.get_data()
        data3 = np.concatenate([e.get_data() for e in [epochs_meg, epochs_eeg]], axis=1)
        assert_array_equal(data1.shape, data2.shape)
        assert_allclose(data1, data3, atol=1e-25)
        assert_allclose(data1, data2, atol=1e-25)

    assert not epochs_meg.times.flags["WRITEABLE"]
    epochs_meg2 = epochs_meg.copy()
    assert not epochs_meg.times.flags["WRITEABLE"]
    assert not epochs_meg2.times.flags["WRITEABLE"]
    epochs_meg2.set_meas_date(0)
    epochs_meg2.copy().add_channels([epochs_eeg])

    epochs_meg2 = epochs_meg.copy()
    epochs2 = epochs_meg.copy().add_channels([epochs_eeg])

    epochs_meg2 = epochs_meg.copy()
    epochs_meg2.events[3, 2] -= 1

    with pytest.raises(ValueError, match="must match"):
        epochs_meg.add_channels([epochs_eeg[:2]])

    epochs_meg2 = epochs_meg.copy()
    with epochs_meg2.info._unlock():
        epochs_meg2.info["sfreq"] += 10
    assert "eeg" not in epochs_meg
    assert "meg" not in epochs_eeg
    with pytest.raises(RuntimeError, match="how to merge"):
        epochs_meg2.add_channels([epochs_eeg])

    epochs_meg2 = epochs_meg.copy()
    with epochs_meg2.info._unlock():
        epochs_meg2.info["chs"][1]["ch_name"] = epochs_meg2.info["ch_names"][0]
    epochs_meg2.info._update_redundant()

    # use delayed projection, add channel, ensure projectors match
    epochs_meg2 = make_epochs(picks=picks_meg, proj="delayed")
    assert len(epochs_meg2.info["projs"]) == 3
    meg2_proj = epochs_meg2._projector
    assert meg2_proj is not None
    epochs_eeg = make_epochs(picks=picks_eeg, proj="delayed")
    epochs_meg2.add_channels([epochs_eeg])
    del epochs_eeg
    assert len(epochs_meg2.info["projs"]) == 3
    new_proj = epochs_meg2._projector
    n_meg, n_eeg = len(picks_meg), len(picks_eeg)
    n_tot = n_meg + n_eeg
    assert new_proj.shape == (n_tot,) * 2
    assert_allclose(new_proj[:n_meg, :n_meg], meg2_proj, atol=1e-12)
    assert_allclose(new_proj[n_meg:, n_meg:], np.eye(n_eeg), atol=1e-12)


def test_array_epochs(tmp_path, browser_backend):
    """Test creating epochs from array."""
    # creating
    data = rng.random_sample((10, 20, 300))
    sfreq = 1e3
    ch_names = [f"EEG {i + 1:03}" for i in range(20)]
    types = ["eeg"] * 20
    info = create_info(ch_names, sfreq, types)
    events = np.c_[np.arange(1, 600, 60), np.zeros(10, int), [1, 2] * 5]
    epochs = EpochsArray(data, info, events, tmin)
    assert epochs.event_id == {"1": 1, "2": 2}
    assert str(epochs).startswith("<EpochsArray")
    # From GH#1963
    with pytest.raises(ValueError, match="number of events must match"):
        EpochsArray(data[:-1], info, events, tmin)
    pytest.raises(ValueError, EpochsArray, data, info, events, tmin, dict(a=1))
    pytest.raises(ValueError, EpochsArray, data, info, events, tmin, selection=[1])
    # should be fine
    EpochsArray(data, info, events, tmin, selection=np.arange(len(events)) + 5)

    # saving
    temp_fname = tmp_path / "test-epo.fif"
    epochs.save(temp_fname, overwrite=True)
    epochs2 = read_epochs(temp_fname)
    data2 = epochs2.get_data()
    assert_allclose(data, data2)
    assert_allclose(epochs.times, epochs2.times)
    assert_equal(epochs.event_id, epochs2.event_id)
    assert_array_equal(epochs.events, epochs2.events)

    # plotting
    epochs[0].plot(events=False)

    # indexing
    assert_array_equal(np.unique(epochs["1"].events[:, 2]), np.array([1]))
    assert_equal(len(epochs[:2]), 2)
    data[0, 5, 150] = 3000
    data[1, :, :] = 0
    data[2, 5, 210] = 3000
    data[3, 5, 260] = 0
    epochs = EpochsArray(
        data,
        info,
        events=events,
        tmin=0,
        reject=dict(eeg=1000),
        flat=dict(eeg=1e-1),
        reject_tmin=0.1,
        reject_tmax=0.2,
    )
    assert_equal(len(epochs), len(events) - 2)
    assert_equal(epochs.drop_log[0], ["EEG 006"])
    assert_equal(len(epochs.drop_log), 10)
    assert_equal(len(epochs.events), len(epochs.selection))

    # baseline
    data = np.ones((10, 20, 300))
    epochs = EpochsArray(data, info, events, tmin=-0.2, baseline=(None, 0))
    ep_data = epochs.get_data()
    assert_array_equal(ep_data, np.zeros_like(ep_data))

    # one time point
    epochs = EpochsArray(data[:, :, :1], info, events=events, tmin=0.0)
    assert_allclose(epochs.times, [0.0])
    assert_allclose(epochs.get_data(), data[:, :, :1])
    epochs.save(temp_fname, overwrite=True)
    epochs_read = read_epochs(temp_fname)
    assert_allclose(epochs_read.times, [0.0])
    assert_allclose(epochs_read.get_data(), data[:, :, :1])

    # event as integer (#2435)
    mask = events[:, 2] == 1
    data_1 = data[mask]
    events_1 = events[mask]
    epochs = EpochsArray(data_1, info, events=events_1, event_id=1, tmin=-0.2)

    # default events
    epochs = EpochsArray(data_1, info)
    assert_array_equal(epochs.events[:, 0], np.arange(len(data_1)))
    assert_array_equal(epochs.events[:, 1], np.zeros(len(data_1), int))
    assert_array_equal(epochs.events[:, 2], np.ones(len(data_1), int))


def test_concatenate_epochs():
    """Test concatenate epochs."""
    raw, events, picks = _get_data()
    epochs = Epochs(
        raw=raw, events=events, event_id=event_id, tmin=tmin, tmax=tmax, picks=picks
    )
    epochs2 = epochs.copy()
    epochs_list = [epochs, epochs2]
    epochs_conc = concatenate_epochs(epochs_list)
    assert epochs_conc.preload
    assert isinstance(epochs_conc, EpochsArray)
    assert_array_equal(epochs_conc.events[:, 0], np.unique(epochs_conc.events[:, 0]))

    expected_shape = list(epochs.get_data().shape)
    expected_shape[0] *= 2
    expected_shape = tuple(expected_shape)

    assert_equal(epochs_conc.get_data().shape, expected_shape)
    assert_equal(epochs_conc.drop_log, epochs.drop_log * 2)

    epochs2 = epochs.copy().load_data()
    with pytest.raises(ValueError, match=r"epochs\[1\].info\['nchan'\] must"):
        concatenate_epochs([epochs, epochs2.copy().drop_channels(epochs2.ch_names[:1])])

    epochs2._set_times(np.delete(epochs2.times, 1))
    with pytest.raises(ValueError, match="could not be broadcast"):
        concatenate_epochs([epochs, epochs2])

    assert_equal(epochs_conc._raw, None)

    # check if baseline is same for all epochs
    epochs2 = epochs.copy()
    epochs2.apply_baseline((-0.1, None))
    with pytest.raises(ValueError, match="Baseline must be same"):
        concatenate_epochs([epochs, epochs2])

    # check if dev_head_t is same
    epochs2 = epochs.copy()
    concatenate_epochs([epochs, epochs2])  # should work
    epochs2.info["dev_head_t"]["trans"][:3, 3] += 0.0001
    with pytest.raises(ValueError, match=r"info\['dev_head_t'\] differs"):
        concatenate_epochs([epochs, epochs2])
    with pytest.raises(TypeError, match="must be a list or tuple"):
        concatenate_epochs("foo")
    with pytest.raises(TypeError, match="must be an instance of Epochs"):
        concatenate_epochs([epochs, "foo"])
    epochs2.info["dev_head_t"] = None
    with pytest.raises(ValueError, match=r"info\['dev_head_t'\] differs"):
        concatenate_epochs([epochs, epochs2])
    epochs.info["dev_head_t"] = None
    concatenate_epochs([epochs, epochs2])  # should work

    # check that different event_id does not work:
    epochs1 = epochs.copy()
    epochs2 = epochs.copy()
    epochs1.event_id = dict(a=1)
    epochs2.event_id = dict(a=2)
    with pytest.raises(ValueError, match="identical keys"):
        concatenate_epochs([epochs1, epochs2])

    # check concatenating epochs where one of the objects is empty
    epochs2 = epochs.copy()[:0]
    with _record_warnings(), pytest.warns(RuntimeWarning, match="was empty"):
        concatenate_epochs([epochs, epochs2])

    # check concatenating epochs results are chronologically ordered
    epochs2 = epochs.copy().load_data()
    # Ensure first event is at 0
    epochs2.events[:, 0] -= np.min(epochs2.events[:, 0])
    with pytest.warns(RuntimeWarning, match="not chronologically ordered"):
        concatenate_epochs([epochs, epochs2], add_offset=False)
    concatenate_epochs([epochs, epochs2], add_offset=True)


@pytest.mark.slowtest
def test_concatenate_epochs_large():
    """Test concatenating epochs on large data."""
    raw, events, picks = _get_data()
    epochs = Epochs(
        raw=raw,
        events=events,
        event_id=event_id,
        tmin=tmin,
        tmax=tmax,
        picks=picks,
        preload=True,
    )

    # check events are shifted, but relative position are equal
    epochs_list = [epochs.copy() for ii in range(3)]
    epochs_cat = concatenate_epochs(epochs_list)
    for ii in range(3):
        evs = epochs_cat.events[ii * len(epochs) : (ii + 1) * len(epochs)]
        rel_pos = epochs_list[ii].events[:, 0] - evs[:, 0]
        assert sum(rel_pos - rel_pos[0]) == 0

    # test large number of epochs
    long_epochs_list = [epochs.copy() for ii in range(60)]
    many_epochs_cat = concatenate_epochs(long_epochs_list)
    max_expected_sample_index = 60 * 1.2 * np.max(epochs.events[:, 0])
    assert np.max(many_epochs_cat.events[:, 0]) < max_expected_sample_index


def test_add_channels():
    """Test epoch splitting / re-appending channel types."""
    raw, events, picks = _get_data()
    epoch_nopre = Epochs(
        raw=raw, events=events, event_id=event_id, tmin=tmin, tmax=tmax, picks=picks
    )
    epoch = Epochs(
        raw=raw,
        events=events,
        event_id=event_id,
        tmin=tmin,
        tmax=tmax,
        picks=picks,
        preload=True,
    )
    epoch_eeg = epoch.copy().pick(picks="eeg")
    epoch_meg = epoch.copy().pick(picks="meg")
    epoch_stim = epoch.copy().pick(picks="stim")
    epoch_eeg_meg = epoch.copy().pick(picks=["meg", "eeg"])
    epoch_new = epoch_meg.copy().add_channels([epoch_eeg, epoch_stim])
    assert all(
        ch in epoch_new.ch_names for ch in epoch_stim.ch_names + epoch_meg.ch_names
    )
    epoch_new = epoch_meg.copy().add_channels([epoch_eeg])

    assert (ch in epoch_new.ch_names for ch in epoch.ch_names)
    assert_array_equal(epoch_new._data, epoch_eeg_meg._data)
    assert all(ch not in epoch_new.ch_names for ch in epoch_stim.ch_names)

    # Now test errors
    epoch_badsf = epoch_eeg.copy()
    with epoch_badsf.info._unlock():
        epoch_badsf.info["sfreq"] = 3.1415927
    epoch_eeg = epoch_eeg.crop(-0.1, 0.1)

    epoch_meg.load_data()
    pytest.raises(RuntimeError, epoch_meg.add_channels, [epoch_nopre])
    pytest.raises(RuntimeError, epoch_meg.add_channels, [epoch_badsf])
    pytest.raises(ValueError, epoch_meg.add_channels, [epoch_eeg])
    pytest.raises(ValueError, epoch_meg.add_channels, [epoch_meg])
    pytest.raises(TypeError, epoch_meg.add_channels, epoch_badsf)


def test_seeg_ecog():
    """Test compatibility of the Epoch object with SEEG, DBS and ECoG data."""
    n_epochs, n_channels, n_times, sfreq = 5, 10, 20, 1000.0
    data = np.ones((n_epochs, n_channels, n_times))
    events = np.array([np.arange(n_epochs), [0] * n_epochs, [1] * n_epochs]).T
    pick_dict = dict(meg=False, exclude=[])
    for key in ("seeg", "dbs", "ecog"):
        info = create_info(n_channels, sfreq, key)
        epochs = EpochsArray(data, info, events)
        pick_dict.update({key: True})
        picks = pick_types(epochs.info, **pick_dict)
        del pick_dict[key]
        assert_equal(len(picks), n_channels)


def test_default_values():
    """Test default event_id, tmax tmin values are working correctly."""
    raw, events = _get_data()[:2]
    epoch_1 = Epochs(raw, events[:1], preload=True)
    epoch_2 = Epochs(raw, events[:1], tmin=-0.2, tmax=0.5, preload=True)
    assert_equal(hash(epoch_1), hash(epoch_2))


def test_metadata(tmp_path, monkeypatch):
    """Test metadata support with pandas."""
    pd = pytest.importorskip("pandas")
    data = np.random.randn(10, 2, 2000)
    chs = ["a", "b"]
    info = create_info(chs, 1000)
    meta = np.array(
        [[1.0] * 5 + [3.0] * 5, ["a"] * 2 + ["b"] * 3 + ["c"] * 3 + ["µ"] * 2],
        dtype="object",
    ).T
    meta = pd.DataFrame(meta, columns=["num", "letter"])
    meta["num"] = np.array(meta["num"], float)
    events = np.arange(meta.shape[0])
    events = np.column_stack([events, np.zeros([len(events), 2])]).astype(int)
    events[5:, -1] = 1
    event_id = {"zero": 0, "one": 1}
    with catch_logging() as log:
        epochs = EpochsArray(
            data, info, metadata=meta, events=events, event_id=event_id, verbose=True
        )
    log = log.getvalue()
    msg = "Adding metadata with 2 columns"
    assert log.count(msg) == 1, f"\nto find:\n{msg}\n\nlog:\n{log}"
    with use_log_level(True):
        with catch_logging() as log:
            epochs.metadata = meta
    log = log.getvalue().strip()
    assert log == "Replacing existing metadata with 2 columns", f"{log}"
    indices = np.arange(len(epochs))  # expected indices
    assert_array_equal(epochs.metadata.index, indices)

    assert len(epochs[[1, 2]].events) == len(epochs[[1, 2]].metadata)
    assert_array_equal(epochs[[1, 2]].metadata.index, indices[[1, 2]])
    assert len(epochs["one"]) == 5

    # Construction
    with pytest.raises(ValueError):
        # Events and metadata must have same len
        epochs_arr = EpochsArray(
            epochs._data,
            epochs.info,
            epochs.events[:-1],
            tmin=0,
            event_id=epochs.event_id,
            metadata=epochs.metadata,
        )

    with pytest.raises(ValueError):
        # Events and data must have same len
        epochs = EpochsArray(data, info, metadata=meta.iloc[:-1])

    for data in [meta.values, meta["num"]]:
        # Metadata must be a DataFrame
        with pytest.raises(ValueError):
            epochs = EpochsArray(data, info, metadata=data)

    # Need strings, ints, and floats
    with pytest.raises(ValueError):
        tmp_meta = meta.copy()
        tmp_meta["foo"] = np.array  # This should be of type object
        epochs = EpochsArray(data, info, metadata=tmp_meta)

    # Getitem
    assert len(epochs["num < 2"]) == 5
    assert len(epochs["num < 5"]) == 10
    assert len(epochs['letter == "b"']) == 3
    assert len(epochs["num < 5"]) == len(epochs["num < 5"].metadata)

    with pytest.raises(KeyError):
        epochs['blah == "yo"']

    assert_array_equal(epochs.selection, indices)
    epochs.drop(0)
    assert_array_equal(epochs.selection, indices[1:])
    assert_array_equal(epochs.metadata.index, indices[1:])
    epochs.drop([0, -1])
    assert_array_equal(epochs.selection, indices[2:-1])
    assert_array_equal(epochs.metadata.index, indices[2:-1])
    assert_array_equal(len(epochs), 7)  # originally 10

    # I/O
    # Make sure values don't change with I/O
    temp_fname = tmp_path / "tmp-epo.fif"
    temp_one_fname = tmp_path / "tmp-one-epo.fif"
    with catch_logging() as log:
        epochs.save(temp_fname, verbose=True, overwrite=True)
    assert log.getvalue() == ""  # assert no junk from metadata setting
    epochs_read = read_epochs(temp_fname, preload=True)
    assert_metadata_equal(epochs.metadata, epochs_read.metadata)
    epochs_arr = EpochsArray(
        epochs._data,
        epochs.info,
        epochs.events,
        tmin=0,
        event_id=epochs.event_id,
        metadata=epochs.metadata,
        selection=epochs.selection,
    )
    assert_metadata_equal(epochs.metadata, epochs_arr.metadata)

    with pytest.raises(TypeError):  # Needs to be a dataframe
        epochs.metadata = np.array([0])

    ###########################################################################
    # Now let's fake having no Pandas and make sure everything works

    epochs_one = epochs["one"]
    epochs_one.save(temp_one_fname, overwrite=True)
    epochs_one_read = read_epochs(temp_one_fname)
    assert_metadata_equal(epochs_one.metadata, epochs_one_read.metadata)

    with monkeypatch.context() as ctx:

        def _check(strict=True):
            if strict:
                raise RuntimeError("Pandas not installed")
            else:
                return False

        ctx.setattr(mne.epochs, "_check_pandas_installed", _check)
        ctx.setattr(mne.utils.mixin, "_check_pandas_installed", _check)

        epochs_read = read_epochs(temp_fname)
        assert isinstance(epochs_read.metadata, list)
        assert isinstance(epochs_read.metadata[0], dict)
        assert epochs_read.metadata[5]["num"] == 3.0

        epochs_one_read = read_epochs(temp_one_fname)
        assert isinstance(epochs_one_read.metadata, list)
        assert isinstance(epochs_one_read.metadata[0], dict)
        assert epochs_one_read.metadata[0]["num"] == 3.0

        epochs_one_nopandas = epochs_read["one"]
        assert epochs_read.metadata[5]["num"] == 3.0
        assert epochs_one_nopandas.metadata[0]["num"] == 3.0
        # sel (no Pandas) == sel (w/ Pandas) -> save -> load (no Pandas)
        assert_metadata_equal(epochs_one_nopandas.metadata, epochs_one_read.metadata)
        epochs_one_nopandas.save(temp_one_fname, overwrite=True)
        # can't make this query
        with pytest.raises(KeyError) as excinfo:
            epochs_read["num < 2"]
            excinfo.match(".*Pandas query could not be performed.*")
        # still can't, but with no metadata the message should be different
        epochs_read.metadata = None
        with pytest.raises(KeyError) as excinfo:
            epochs_read["num < 2"]
            excinfo.match(r"^((?!Pandas).)*$")
        del epochs_read
        # sel (no Pandas) == sel (no Pandas) -> save -> load (no Pandas)
        epochs_one_nopandas_read = read_epochs(temp_one_fname)
        assert_metadata_equal(
            epochs_one_nopandas_read.metadata, epochs_one_nopandas.metadata
        )
    # sel (w/ Pandas) == sel (no Pandas) -> save -> load (w/ Pandas)
    epochs_one_nopandas_read = read_epochs(temp_one_fname)
    assert_metadata_equal(epochs_one_nopandas_read.metadata, epochs_one.metadata)

    # gh-4820
    raw_data = np.random.randn(10, 1000)
    info = mne.create_info(10, 1000.0)
    raw = mne.io.RawArray(raw_data, info)
    events = [[0, 0, 1], [100, 0, 1], [200, 0, 1], [300, 0, 1]]
    metadata = pd.DataFrame([dict(idx=idx) for idx in range(len(events))])
    epochs = mne.Epochs(raw, events=events, tmin=-0.050, tmax=0.100, metadata=metadata)
    epochs.drop_bad()
    assert len(epochs) == len(epochs.metadata)

    # gh-4821
    epochs.metadata["new_key"] = 1
    assert_array_equal(epochs["new_key == 1"].get_data(), epochs.get_data())
    # ensure bad user changes break things
    epochs.metadata.drop(epochs.metadata.index[2], inplace=True)
    assert len(epochs.metadata) == len(epochs) - 1
    with pytest.raises(
        ValueError, match="metadata must have the same number of rows .*"
    ):
        epochs["new_key == 1"]

    # metadata should be same length as original events
    raw_data = np.random.randn(2, 10000)
    info = mne.create_info(2, 1000.0)
    raw = mne.io.RawArray(raw_data, info)
    opts = dict(raw=raw, tmin=0, tmax=0.001, baseline=None)
    events = [[0, 0, 1], [1, 0, 2]]
    metadata = pd.DataFrame(events, columns=["onset", "duration", "value"])
    epochs = Epochs(events=events, event_id=1, metadata=metadata, **opts)
    epochs.drop_bad()
    assert len(epochs) == 1
    assert len(epochs.metadata) == 1
    with pytest.raises(ValueError, match="same number of rows"):
        Epochs(events=events, event_id=1, metadata=metadata.iloc[:1], **opts)

    # gh-7732: problem when repeated events and metadata
    for er in ("drop", "merge"):
        events = [[1, 0, 1], [1, 0, 1]]
        epochs = Epochs(events=events, event_repeated=er, **opts)
        epochs.drop_bad()
        assert len(epochs) == 1
        events = [[1, 0, 1], [1, 0, 1]]
        epochs = Epochs(events=events, event_repeated=er, metadata=metadata, **opts)
        epochs.drop_bad()
        assert len(epochs) == 1
        assert len(epochs.metadata) == 1

    # gh-10705: support boolean columns
    metadata = pd.DataFrame(
        {"A": pd.Series([True, True, True, False, False, pd.NA], dtype="boolean")}
    )
    rng = np.random.default_rng()
    epochs = mne.EpochsArray(
        data=rng.standard_normal(size=(6, 8, 500)),
        info=mne.create_info(8, 250, "eeg"),
        event_id={"A": 1},
        metadata=metadata,
    )

    assert len(epochs["A"]) == 6  # epochs of event type A
    assert len(epochs["A == True"]) == 3  # epochs for which column A == True
    assert len(epochs["not A"]) == 2  # epochs for which column A == False
    assert len(epochs["A.isna()"]) == 1  # epochs for NA in column A


def assert_metadata_equal(got, exp):
    """Assert metadata are equal."""
    if exp is None:
        assert got is None
    elif isinstance(exp, list):
        assert isinstance(got, list)
        assert len(got) == len(exp)
        for ii, (g, e) in enumerate(zip(got, exp)):
            assert list(g.keys()) == list(e.keys())
        for key in g.keys():
            assert g[key] == e[key], (ii, key)
    else:  # DataFrame
        import pandas

        assert isinstance(exp, pandas.DataFrame)
        assert isinstance(got, pandas.DataFrame)
        assert set(got.columns) == set(exp.columns)
        check = got == exp
        assert check.all().all()


@pytest.mark.parametrize(
    ("all_event_id", "row_events", "tmin", "tmax", "keep_first", "keep_last"),
    [
        (
            {"a/1": 1, "a/2": 2, "b/1": 3, "b/2": 4, "c": 32},  # all events
            None,
            -0.5,
            1.5,
            None,
            None,
        ),
        ({"a/1": 1, "a/2": 2}, None, -0.5, 1.5, None, None),  # subset of events
        (dict(), None, -0.5, 1.5, None, None),  # empty set of events
        (
            {"a/1": 1, "a/2": 2, "b/1": 3, "b/2": 4, "c": 32},
            ("a/1", "a/2", "b/1", "b/2"),
            -0.5,
            1.5,
            ("a", "b"),
            "c",
        ),
        # Test when tmin, tmax are None
        ({"a/1": 1, "a/2": 2}, None, None, 1.5, None, None),  # tmin is None
        ({"a/1": 1, "a/2": 2}, None, -0.5, None, None, None),  # tmax is None
        ({"a/1": 1, "a/2": 2}, None, None, None, None, None),  # tmin and tmax are None
    ],
)
def test_make_metadata(all_event_id, row_events, tmin, tmax, keep_first, keep_last):
    """Test that make_metadata works."""
    pytest.importorskip("pandas")
    raw, all_events, _ = _get_data()
    sfreq = raw.info["sfreq"]
    kwargs = dict(
        events=all_events,
        event_id=all_event_id,
        row_events=row_events,
        keep_first=keep_first,
        keep_last=keep_last,
        tmin=tmin,
        tmax=tmax,
        sfreq=sfreq,
    )

    if not kwargs["event_id"]:
        with pytest.raises(ValueError, match="must contain at least one"):
            make_metadata(**kwargs)
        return

    metadata, events, event_id = make_metadata(**kwargs)

    assert len(metadata) == len(events)

    if row_events:
        assert set(metadata["event_name"]) == set(row_events)
    else:
        assert set(metadata["event_name"]) == set(event_id.keys())

    # Check we have columns all events
    keep_first = [] if keep_first is None else keep_first
    keep_last = [] if keep_last is None else keep_last
    event_names = sorted(set(event_id.keys()) | set(keep_first) | set(keep_last))

    for event_name in event_names:
        assert event_name in metadata.columns

    # Check the time-locked event's metadata
    for _, row in metadata.iterrows():
        event_name = row["event_name"]
        assert np.isclose(row[event_name], 0)

    # Check non-time-locked events' metadata
    for row_idx, row in metadata.iterrows():
        event_names = sorted(
            set(event_id.keys())
            | set(keep_first)
            | set(keep_last) - set(row["event_name"])
        )
        for event_name in event_names:
            if event_name in keep_first or event_name in keep_last:
                assert isinstance(row[event_name], float)
                if not (
                    (event_name == "a" and row_idx == 30)
                    or (event_name == "b" and row_idx == 14)
                    or (event_name == "c" and row_idx != 16)
                ):
                    assert not np.isnan(row[event_name])

            if event_name in keep_first and event_name not in all_event_id:
                assert row[f"first_{event_name}"] is None or isinstance(
                    row[f"first_{event_name}"], str
                )
            elif event_name in keep_last and event_name not in all_event_id:
                assert row[f"last_{event_name}"] is None or isinstance(
                    row[f"last_{event_name}"], str
                )

    Epochs(raw, events=events, event_id=event_id, metadata=metadata, verbose="warning")


@pytest.mark.parametrize(
    ("tmin", "tmax"),
    [
        (None, None),
        ("cue", "resp"),
        (["cue"], ["resp"]),
        (None, "resp"),
        ("cue", None),
        (["rec_start", "cue"], ["resp", "rec_end"]),
    ],
)
def test_make_metadata_bounded_by_row_or_tmin_tmax_event_names(tmin, tmax):
    """Test make_metadata() with tmin, tmax set to None or strings."""
    pytest.importorskip("pandas")

    sfreq = 100
    duration = 15
    n_chs = 10

    # Define events and generate annotations
    experimental_events = [
        # Beginning of recording until response (1st trial)
        {"onset": 0.0, "description": "rec_start", "duration": 1 / sfreq},
        {"onset": 1.0, "description": "cue", "duration": 1 / sfreq},
        {"onset": 2.0, "description": "stim", "duration": 1 / sfreq},
        {"onset": 2.5, "description": "resp", "duration": 1 / sfreq},
        # 2nd trial
        {"onset": 4.0, "description": "cue", "duration": 1 / sfreq},
        {"onset": 4.3, "description": "stim", "duration": 1 / sfreq},
        {"onset": 8.0, "description": "resp", "duration": 1 / sfreq},
        # 3rd trial until end of the recording
        {"onset": 10.0, "description": "cue", "duration": 1 / sfreq},
        {"onset": 12.0, "description": "stim", "duration": 1 / sfreq},
        {"onset": 13.0, "description": "resp", "duration": 1 / sfreq},
        {"onset": 14.9, "description": "rec_end", "duration": 1 / sfreq},
    ]

    annots = mne.Annotations(
        onset=[e["onset"] for e in experimental_events],
        description=[e["description"] for e in experimental_events],
        duration=[e["duration"] for e in experimental_events],
    )

    # Generate raw data, attach the annotations, and convert to events
    rng = np.random.default_rng()
    data = 1e-5 * rng.standard_normal((n_chs, sfreq * duration))
    info = mne.create_info(
        ch_names=[f"EEG {i}" for i in range(n_chs)], sfreq=sfreq, ch_types="eeg"
    )

    raw = mne.io.RawArray(data=data, info=info)
    raw.set_annotations(annots)
    events, event_id = mne.events_from_annotations(raw=raw)

    metadata, events_new, _ = mne.epochs.make_metadata(
        events=events,
        event_id=event_id,
        tmin=tmin,
        tmax=tmax,
        sfreq=raw.info["sfreq"],
        row_events="cue",
    )

    # We should have 3 rows in the metadata table in total.
    # rec_start occurred before the first row_event, so should not be included
    # rec_end occurred after the last row_event and should be included

    assert len(metadata) == 3
    assert (metadata["event_name"] == "cue").all()
    assert (metadata["cue"] == 0.0).all()

    for row in metadata.itertuples():
        assert row.cue < row.stim < row.resp
        assert np.isnan(row.rec_start)

    # Beginning of recording until end of 1st trial
    assert np.isnan(metadata.iloc[0]["rec_end"])

    # 2nd trial
    assert np.isnan(metadata.iloc[1]["rec_end"])

    # 3rd trial
    if tmax is None:
        # until end of the recording
        assert metadata.iloc[2]["resp"] < metadata.iloc[2]["rec_end"]
    else:
        # until tmax
        assert np.isnan(metadata.iloc[2]["rec_end"])
        last_event_name = tmax[0] if isinstance(tmax, list) else tmax
        assert metadata.iloc[2][last_event_name] > 0


def test_events_list():
    """Test that events can be a list."""
    events = [[100, 0, 1], [200, 0, 1], [300, 0, 1]]
    epochs = mne.Epochs(
        mne.io.RawArray(np.random.randn(10, 1000), mne.create_info(10, 1000.0)),
        events=events,
    )
    assert_array_equal(epochs.events, np.array(events))
    assert repr(epochs)  # test repr
    assert epochs._repr_html_()  # test _repr_html_


def test_save_overwrite(tmp_path):
    """Test saving with overwrite functionality."""
    raw = mne.io.RawArray(
        np.random.RandomState(0).randn(100, 10000), mne.create_info(100, 1000.0)
    )

    events = mne.make_fixed_length_events(raw, 1)
    epochs = mne.Epochs(raw, events)

    # scenario 1: overwrite=False and there isn't a file to overwrite
    # make a filename that has not already been saved to
    fname1 = tmp_path / "test_v1-epo.fif"
    # run function to be sure it doesn't throw an error
    epochs.save(fname1, overwrite=False)
    # check that the file got written
    assert fname1.is_file()

    # scenario 2: overwrite=False and there is a file to overwrite
    # fname1 exists because of scenario 1 above
    with pytest.raises(OSError, match="Destination file exists."):
        epochs.save(fname1, overwrite=False)

    # scenario 3: overwrite=True and there isn't a file to overwrite
    # make up a filename that has not already been saved to
    fname2 = tmp_path / "test_v2-epo.fif"
    # run function to be sure it doesn't throw an error
    epochs.save(fname2, overwrite=True)
    # check that the file got written
    assert fname2.is_file()
    with pytest.raises(OSError, match="exists"):
        epochs.save(fname2)

    # scenario 4: overwrite=True and there is a file to overwrite
    # run function to be sure it doesn't throw an error
    # fname2 exists because of scenario 1 above
    epochs.save(fname2, overwrite=True)


@pytest.mark.parametrize("preload", (True, False))
@pytest.mark.parametrize("is_complex", (True, False))
@pytest.mark.parametrize("fmt, rtol", [("single", 2e-6), ("double", 1e-10)])
def test_save_complex_data(tmp_path, preload, is_complex, fmt, rtol):
    """Test whether epochs of hilbert-transformed data can be saved."""
    raw, events = _get_data()[:2]
    raw.load_data()
    if is_complex:
        raw.apply_hilbert(envelope=False, n_fft=None)
    epochs = Epochs(raw, events[:1], preload=True)[0]
    temp_fname = tmp_path / "test-epo.fif"
    epochs.save(temp_fname, fmt=fmt)
    data = epochs.get_data().copy()
    epochs_read = read_epochs(temp_fname, proj=False, preload=preload)
    data_read = epochs_read.get_data()
    want_dtype = np.complex128 if is_complex else np.float64
    assert data.dtype == want_dtype
    assert data_read.dtype == want_dtype
    # XXX for some reason some random samples in here are off by a larger
    # factor...
    if fmt == "single" and not preload and not is_complex:
        rtol = 2e-4
    assert_allclose(data_read, data, rtol=rtol)


def test_no_epochs(tmp_path):
    """Test that having the first epoch bad does not break writing."""
    # a regression noticed in #5564
    raw, events = _get_data()[:2]
    reject = dict(grad=4000e-13, mag=4e-12, eog=150e-6)
    raw.info["bads"] = ["MEG 2443", "EEG 053"]
    epochs = mne.Epochs(raw, events, reject=reject)
    epochs.save(tmp_path / "sample-epo.fif", overwrite=True)
    assert 0 not in epochs.selection
    assert len(epochs) > 0
    # and with no epochs remaining
    raw.info["bads"] = []
    epochs = mne.Epochs(raw, events, reject=reject)
    with _record_warnings(), pytest.warns(RuntimeWarning, match="no data"):
        epochs.save(tmp_path / "sample-epo.fif", overwrite=True)
    assert len(epochs) == 0  # all dropped


def test_readonly_times():
    """Test that the times property is read only."""
    raw, events = _get_data()[:2]
    epochs = Epochs(raw, events[:1], preload=True)
    with pytest.raises(ValueError, match="read-only"):
        epochs._times_readonly += 1
    with pytest.raises(ValueError, match="read-only"):
        epochs.times += 1
    with pytest.raises(ValueError, match="read-only"):
        epochs.times[:] = 0.0


def test_channel_types_mixin():
    """Test channel types mixin."""
    raw, events = _get_data()[:2]
    epochs = Epochs(raw, events[:1], preload=True)
    ch_types = epochs.get_channel_types()
    assert len(ch_types) == len(epochs.ch_names)
    assert all(np.isin(ch_types, ["mag", "grad", "eeg", "eog", "stim"]))


def test_average_methods():
    """Test average methods."""
    n_epochs, n_channels, n_times = 5, 10, 20
    sfreq = 1000.0
    data = rng.randn(n_epochs, n_channels, n_times)

    events = np.array([np.arange(n_epochs), [0] * n_epochs, [1] * n_epochs]).T
    # Add second event type
    events[-2:, 2] = 2
    event_id = dict(first=1, second=2)

    info = create_info(n_channels, sfreq, "eeg")
    epochs = EpochsArray(data, info, events, event_id=event_id)

    for method in ("mean", "median"):
        if method == "mean":

            def fun(data):
                return np.mean(data, axis=0)

        elif method == "median":

            def fun(data):
                return np.median(data, axis=0)

        evoked_data = epochs.average(method=method).data
        assert_array_equal(evoked_data, fun(data))

    # Test averaging by event type
    ev = epochs.average(by_event_type=True)
    assert len(ev) == 2
    assert ev[0].comment == "first"
    assert_array_equal(ev[0].data, np.mean(data[:-2], axis=0))
    assert ev[1].comment == "second"
    assert_array_equal(ev[1].data, np.mean(data[-2:], axis=0))


@pytest.mark.parametrize("relative", (True, False))
def test_shift_time(relative):
    """Test the timeshift method."""
    timeshift = 13.5e-3  # Using sub-ms timeshift to test for sample accuracy.
    raw, events = _get_data()[:2]
    epochs = Epochs(raw, events[:1], preload=True, baseline=None)
    avg = epochs.average().shift_time(timeshift, relative=relative)
    avg2 = epochs.shift_time(timeshift, relative=relative).average()
    assert_array_equal(avg.times, avg2.times)
    assert_equal(avg.first, avg2.first)
    assert_equal(avg.last, avg2.last)
    assert_array_equal(avg.data, avg2.data)


@pytest.mark.parametrize("preload", (True, False))
def test_shift_time_raises_when_not_loaded(preload):
    """Test whether shift_time throws an exception when data is not loaded."""
    timeshift = 13.5e-3  # Using sub-ms timeshift to test for sample accuracy.
    raw, events = _get_data()[:2]
    epochs = Epochs(raw, events[:1], preload=preload, baseline=None)
    if not preload:
        pytest.raises(RuntimeError, epochs.shift_time, timeshift)
    else:
        epochs.shift_time(timeshift)


@testing.requires_testing_data
@pytest.mark.parametrize("preload", (True, False))
@pytest.mark.parametrize("fname", (fname_raw_testing, raw_fname))
def test_epochs_drop_selection(fname, preload):
    """Test epochs drop and selection."""
    raw = read_raw_fif(fname, preload=True)
    raw.info["bads"] = ["MEG 2443"]
    events = mne.make_fixed_length_events(raw, id=1, start=0.5, duration=1.0)
    assert len(events) > 10
    kwargs = dict(tmin=-0.2, tmax=0.5, proj=False, baseline=(None, 0))
    reject = dict(mag=4e-12, grad=4000e-13)

    # Hack the first channel data to store the desired selection in epoch data
    raw._data[0] = 0.0
    scale = 1e-13
    vals = scale * np.arange(1, len(events) + 1)
    raw._data[0, events[:, 0] - raw.first_samp + 1] = vals

    def _get_selection(epochs):
        """Get the desired selection from our modified epochs."""
        selection = np.round(epochs.get_data()[:, 0].max(axis=-1) / scale)
        return selection.astype(int) - 1

    # No rejection
    epochs = mne.Epochs(raw, events, preload=preload, **kwargs)
    if not preload:
        epochs.drop_bad()
    assert len(epochs) == len(events)  # none dropped
    selection = _get_selection(epochs)
    assert_array_equal(np.arange(len(events)), selection)  # kept all
    assert_array_equal(epochs.selection, selection)

    # Dropping during construction
    epochs = mne.Epochs(raw, events, preload=preload, reject=reject, **kwargs)
    if not preload:
        epochs.drop_bad()
    assert 4 < len(epochs) < len(events)  # some dropped
    selection = _get_selection(epochs)
    assert_array_equal(selection, epochs.selection)
    good_selection = selection

    # Dropping after construction
    epochs = mne.Epochs(raw, events, preload=preload, **kwargs)
    if not preload:
        epochs.drop_bad()
    assert len(epochs) == len(events)
    epochs.drop_bad(reject=reject, verbose=True)
    assert_array_equal(epochs.selection, good_selection)  # same as before
    selection = _get_selection(epochs)
    assert_array_equal(selection, epochs.selection)

    # Dropping after construction manually
    epochs = mne.Epochs(raw, events, preload=preload, **kwargs)
    if not preload:
        epochs.drop_bad()
    assert_array_equal(epochs.selection, np.arange(len(events)))  # no drops
    drop_idx = [1, 3]
    want_selection = np.setdiff1d(np.arange(len(events)), drop_idx)
    epochs.drop(drop_idx)
    assert_array_equal(epochs.selection, want_selection)
    selection = np.round(epochs.get_data()[:, 0].max(axis=-1) / scale)
    selection = selection.astype(int) - 1
    assert_array_equal(selection, epochs.selection)


@pytest.mark.parametrize("kind", ("file", "bytes"))
@pytest.mark.parametrize("preload", (True, False))
def test_file_like(kind, preload, tmp_path):
    """Test handling with file-like objects."""
    raw = mne.io.RawArray(
        np.random.RandomState(0).randn(100, 10000), mne.create_info(100, 1000.0)
    )
    events = mne.make_fixed_length_events(raw, 1)
    epochs = mne.Epochs(raw, events, preload=preload)
    fname = tmp_path / "test-epo.fif"
    epochs.save(fname, overwrite=True)

    with open(fname, "rb") as file_fid:
        fid = BytesIO(file_fid.read()) if kind == "bytes" else file_fid
        assert not fid.closed
        assert not file_fid.closed
        with pytest.raises(ValueError, match="preload must be used with file"):
            read_epochs(fid, preload=False)
        assert not fid.closed
        assert not file_fid.closed
    assert file_fid.closed


@pytest.mark.parametrize("preload", (True, False))
def test_epochs_get_data_item(preload):
    """Test epochs.get_data(item=...)."""
    raw, events, _ = _get_data()
    epochs = Epochs(raw, events[:10], event_id, tmin, tmax, preload=preload)
    if not preload:
        with pytest.raises(ValueError, match="item must be None"):
            epochs.get_data(item=0)
        epochs.drop_bad()
    one_data = epochs.get_data(item=0)
    one_epo = epochs[0]
    assert_array_equal(one_data, one_epo.get_data())


def test_pick_types_reject_flat_keys():
    """Test that epochs.pick_types removes keys from reject/flat."""
    raw, events, _ = _get_data()
    event_id = {"a/1": 1, "a/2": 2, "b/1": 3, "b/2": 4}
    picks = pick_types(raw.info, meg=True, eeg=True, ecg=True, eog=True)
    epochs = Epochs(
        raw,
        events,
        event_id,
        preload=True,
        picks=picks,
        reject=dict(grad=1e-9, mag=1e-10, eeg=1e-3, eog=1e-3),
        flat=dict(grad=1e-16, mag=1e-16, eeg=1e-16, eog=1e-16),
    )

    assert sorted(epochs.reject.keys()) == ["eeg", "eog", "grad", "mag"]
    assert sorted(epochs.flat.keys()) == ["eeg", "eog", "grad", "mag"]
    epochs.pick(picks="meg")
    assert sorted(epochs.reject.keys()) == ["grad", "mag"]
    assert sorted(epochs.flat.keys()) == ["grad", "mag"]


@testing.requires_testing_data
def test_make_fixed_length_epochs():
    """Test dividing raw data into equal-sized consecutive epochs."""
    raw = read_raw_fif(raw_fname, preload=True)
    epochs = make_fixed_length_epochs(raw, duration=1, preload=True)
    # Test Raw with annotations
    annot = Annotations(onset=[0], duration=[5], description=["BAD"])
    raw_annot = raw.set_annotations(annot)
    epochs_annot = make_fixed_length_epochs(raw_annot, duration=1.0, preload=True)
    assert len(epochs) > 10
    assert len(epochs_annot) > 10
    assert len(epochs) > len(epochs_annot)

    # overlaps
    epochs = make_fixed_length_epochs(raw, duration=1)
    assert len(epochs.events) > 10
    epochs_ol = make_fixed_length_epochs(raw, duration=1, overlap=0.5)
    assert len(epochs_ol.events) > 20
    epochs_ol_2 = make_fixed_length_epochs(raw, duration=1, overlap=0.9)
    assert len(epochs_ol_2.events) > 100
    assert_array_equal(epochs_ol_2.events[:, 0], np.unique(epochs_ol_2.events[:, 0]))
    with pytest.raises(ValueError, match="overlap must be"):
        make_fixed_length_epochs(raw, duration=1, overlap=1.1)

    # id
    epochs = make_fixed_length_epochs(raw, duration=1, preload=True, id=2)
    assert "2" in epochs.event_id and len(epochs.event_id) == 1


def test_epochs_huge_events(tmp_path):
    """Test epochs with event numbers that are too large."""
    data = np.zeros((1, 1, 1000))
    info = create_info(1, 1000.0, "eeg")
    events = np.array([0, 0, 2147483648], np.int64)
    with pytest.raises(ValueError, match=r"shape \(N, 3\)"):
        EpochsArray(data, info, events)
    events = events[np.newaxis]
    with pytest.raises(ValueError, match="must not exceed"):
        EpochsArray(data, info, events)
    epochs = EpochsArray(data, info)
    epochs.events = events
    with pytest.raises(TypeError, match="exceeds maximum"):
        epochs.save(tmp_path / "temp-epo.fif")


def _old_bad_write(fid, kind, arr):
    if kind == FIFF.FIFF_MNE_EVENT_LIST:
        arr = arr.copy()
        arr[0, -1] = -1000  # it's transposed
    return write_int(fid, kind, arr)


def test_concat_overflow(tmp_path, monkeypatch):
    """Test overflow events during concat."""
    data = np.zeros((2, 10, 1000))
    events = np.array([[0, 0, 1], [INT32_MAX, 0, 2]])
    info = mne.create_info(10, 1000.0, "eeg")
    epochs_1 = mne.EpochsArray(data, info, events)
    epochs_2 = mne.EpochsArray(data, info, events)
    with pytest.warns(RuntimeWarning, match="consecutive increasing"):
        epochs = mne.concatenate_epochs((epochs_1, epochs_2))
    assert_array_less(0, epochs.events[:, 0])
    fname = tmp_path / "temp-epo.fif"
    epochs.save(fname)
    epochs = read_epochs(fname)
    assert_array_less(0, epochs.events[:, 0])
    assert_array_less(epochs.events[:, 0], INT32_MAX + 1)
    # with our old behavior
    monkeypatch.setattr(mne.epochs, "write_int", _old_bad_write)
    epochs.save(fname, overwrite=True)
    with pytest.warns(RuntimeWarning, match="Incorrect events"):
        epochs = read_epochs(fname)
    assert_array_less(0, epochs.events[:, 0])
    assert_array_less(epochs.events[:, 0], INT32_MAX + 1)


def test_epochs_baseline_after_cropping(tmp_path):
    """Epochs.baseline should be retained if baseline period was cropped."""
    sfreq = 1000
    tstep = 1.0 / sfreq
    times = np.arange(0, 2 + tstep, tstep)

    # Linear ramp: 0–100 µV
    data = (
        scipy.signal.sawtooth(2 * np.pi * 0.25 * times, 0.5).reshape(1, -1)
    ) * 50e-6 + 50e-6

    ch_names = ["EEG 001"]
    ch_types = ["eeg"]
    info = mne.create_info(ch_names=ch_names, sfreq=sfreq, ch_types=ch_types)
    raw = mne.io.RawArray(data, info)

    event_id = dict(event=1)
    events = np.array([[1000, 0, event_id["event"]]])
    epochs_orig = mne.Epochs(
        raw=raw,
        events=events,
        event_id=event_id,
        tmin=-0.2,
        tmax=0.2,
        baseline=(-0.1, 0.1),
    )

    # Assert baseline correction is working as intended.
    samp_min = 1000 - 200
    samp_max = 1000 + 200
    expected_data = data.copy()[0, samp_min : samp_max + 1]
    baseline = expected_data[100:301]
    expected_data -= baseline.mean()
    expected_data = expected_data.reshape(1, 1, -1)
    assert_equal(epochs_orig.get_data(), expected_data)
    del expected_data, baseline, samp_min, samp_max

    # Even after cropping the baseline period, Epochs.baseline should remain
    # unchanged
    epochs_cropped = epochs_orig.copy().load_data().crop(tmin=0, tmax=None)

    assert_equal(epochs_orig.baseline, epochs_cropped.baseline)
    assert "baseline period was cropped" in str(epochs_cropped)
    assert_equal(
        epochs_cropped.get_data().squeeze(), epochs_orig.get_data().squeeze()[200:]
    )

    # Test I/O roundtrip.
    epochs_fname = tmp_path / "temp-cropped-epo.fif"
    epochs_cropped.save(epochs_fname)
    epochs_cropped_read = mne.read_epochs(epochs_fname)

    assert_allclose(epochs_orig.baseline, epochs_cropped_read.baseline)
    assert "baseline period was cropped" in str(epochs_cropped_read)
    assert_allclose(epochs_cropped.get_data(), epochs_cropped_read.get_data())


def test_empty_constructor():
    """Test empty constructor for RtEpochs."""
    info = create_info(1, 1000.0, "eeg")
    event_id = 1
    tmin, tmax, baseline = -0.2, 0.5, None
    BaseEpochs(info, None, None, event_id, tmin, tmax, baseline)


def test_apply_function():
    """Test apply function to epoch objects."""
    n_channels = 10
    data = np.arange(2 * n_channels * 1000).reshape(2, n_channels, 1000)
    events = np.array([[0, 0, 1], [INT32_MAX, 0, 2]])
    info = mne.create_info(n_channels, 1000.0, "eeg")
    epochs = mne.EpochsArray(data, info, events)
    data_epochs = epochs.get_data()

    # apply_function to all channels at once
    def fun(data):
        """Reverse channel order without changing values."""
        return np.eye(data.shape[1])[::-1] @ data

    want = data_epochs[:, ::-1]
    got = epochs.apply_function(fun, channel_wise=False).get_data()
    assert_array_equal(want, got)

    # apply_function channel-wise (to first 3 channels) by replacing with mean
    picks = np.arange(3)
    non_picks = np.arange(3, n_channels)

    def fun(data):
        return np.full_like(data, data.mean())

    out = epochs.apply_function(fun, picks=picks, channel_wise=True)
    expected = epochs.get_data(picks).mean(axis=-1, keepdims=True)
    assert np.all(out.get_data(picks) == expected)
    assert_array_equal(out.get_data(non_picks), epochs.get_data(non_picks))


def test_apply_function_epo_ch_access():
    """Test ch-access within apply function to epoch objects."""

    def _bad_ch_idx(x, ch_idx):
        assert x.shape == (46,)
        assert x[0] == ch_idx
        return x

    def _bad_ch_name(x, ch_name):
        assert x.shape == (46,)
        assert isinstance(ch_name, str)
        assert x[0] == float(ch_name)
        return x

    data = np.full((2, 100), np.arange(2).reshape(-1, 1))
    raw = RawArray(data, create_info(2, 1.0, "mag"))
    ev = np.array([[0, 0, 33], [50, 0, 33]])
    ep = Epochs(raw, ev, tmin=0, tmax=45, baseline=None, preload=True)

    # test ch_idx access in both code paths (parallel / 1 job)
    ep.apply_function(_bad_ch_idx)
    ep.apply_function(_bad_ch_idx, n_jobs=2)
    ep.apply_function(_bad_ch_name)
    ep.apply_function(_bad_ch_name, n_jobs=2)

    # test input catches
    with pytest.raises(
        ValueError,
        match="cannot access.*when channel_wise=False",
    ):
        ep.apply_function(_bad_ch_idx, channel_wise=False)


@testing.requires_testing_data
def test_add_channels_picks():
    """Check that add_channels properly deals with picks."""
    raw = mne.io.read_raw_fif(raw_fname, verbose=False)
    raw.pick([2, 3, 310])  # take some MEG and EEG
    raw.info.normalize_proj()

    events = mne.make_fixed_length_events(raw, id=3000, start=0)
    epochs = mne.Epochs(
        raw,
        events,
        event_id=3000,
        tmin=0,
        tmax=1,
        proj=True,
        baseline=None,
        reject=None,
        preload=True,
        decim=1,
    )

    epochs_final = epochs.copy()
    epochs_bis = epochs.copy().rename_channels(lambda ch: ch + "_bis")
    epochs_final.add_channels([epochs_bis], force_update_info=True)
    epochs_final.drop_channels(epochs.ch_names)


@pytest.mark.parametrize("first_samp", [0, 10])
@pytest.mark.parametrize(
    "meas_date, orig_date, with_extras",
    [
        [None, None, False],
        [np.pi, None, False],
        [np.pi, timedelta(seconds=1), False],
        [None, None, True],
    ],
)
def test_epoch_annotations(first_samp, meas_date, orig_date, with_extras, tmp_path):
    """Test Epoch Annotations from RawArray with dates.

    Tests the following cases crossed with each other:
    - with and without first_samp
    - with and without meas_date
    - with and without an orig_time set in Annotations
    """
    pytest.importorskip("pandas")
    from pandas.testing import assert_frame_equal

    data = np.random.randn(2, 400) * 10e-12
    info = create_info(ch_names=["MEG1", "MEG2"], ch_types="grad", sfreq=100.0)

    # create a Raw object with a first_samp
    raw = RawArray(data.copy(), info, first_samp=first_samp)
    meas_date = _handle_meas_date(meas_date)
    raw.set_meas_date(meas_date)

    # handle orig_date
    if orig_date is not None:
        orig_date = meas_date + orig_date
    ant_dur = 0.1
    extras_row0 = {"foo1": 1, "foo2": 1.1, "foo3": "a", "foo4": None}
    extras = [extras_row0, None, None] if with_extras else None
    ants = Annotations(
        onset=[1.1, 1.2, 2.1],
        duration=[ant_dur, ant_dur, ant_dur],
        description=["x", "y", "z"],
        orig_time=orig_date,
        extras=extras,
    )
    raw.set_annotations(ants)
    epochs = make_fixed_length_epochs(raw, duration=1, overlap=0.5)

    # add Annotations to Epochs metadata
    epochs.add_annotations_to_metadata(with_extras=with_extras)
    metadata = epochs.metadata
    assert "annot_onset" in metadata.columns
    assert "annot_duration" in metadata.columns
    assert "annot_description" in metadata.columns
    if with_extras:
        assert all(f"annot_{k}" in metadata.columns for k in extras_row0.keys())

    # Test that writing and reading back these new metadata works
    temp_fname = tmp_path / "test-epo.fif"
    epochs.save(temp_fname)
    epochs_read = mne.read_epochs(temp_fname)
    assert_metadata_equal(epochs.metadata, epochs_read.metadata)

    # check that the annotations themselves should be equivalent
    # because first_samp offsetting occurs in Raw
    assert_array_equal(raw.annotations.onset, epochs.annotations.onset)
    assert_array_equal(raw.annotations.duration, epochs.annotations.duration)
    assert_array_equal(raw.annotations.description, epochs.annotations.description)

    # compare Epoch annotations with expected values
    epoch_ants = epochs.get_annotations_per_epoch()
    if orig_date is None:
        expected_annot_times = [
            [],
            [[0.6, ant_dur, "x"], [0.7, ant_dur, "y"]],
            [[0.1, ant_dur, "x"], [0.2, ant_dur, "y"]],
            [[0.6, ant_dur, "z"]],
            [[0.1, ant_dur, "z"]],
            [],
            [],
        ]
    else:
        expected_annot_times = [
            [],
            [],
            [],
            [[0.6, ant_dur, "x"], [0.7, ant_dur, "y"]],
            [[0.1, ant_dur, "x"], [0.2, ant_dur, "y"]],
            [[0.6, ant_dur, "z"]],
            [[0.1, ant_dur, "z"]],
        ]
    assert len(expected_annot_times) == len(epoch_ants)
    for x, y in zip(epoch_ants, expected_annot_times):
        if orig_date is not None:
            # when orig_date is set + first_samp, those will offset
            # the onset when Raw sets annotations. These should
            # then be offset accordingly when Epochs look for annotations
            assert_array_almost_equal(
                [_x[0] for _x in x], [_y[0] - raw._first_time for _y in y]
            )
        else:
            # onset relative to Epoch start
            assert_array_almost_equal([_x[0] for _x in x], [_y[0] for _y in y])

        # duration
        assert_array_equal([_x[1] for _x in x], [_y[1] for _y in y])

        # description should be exactly the same
        assert_array_equal([_x[2] for _x in x], [_y[2] for _y in y])

    # metadata should match after resampling
    epochs.load_data()
    epochs.add_annotations_to_metadata(overwrite=True)
    metadata = epochs.metadata.copy()
    epochs.resample(epochs.info["sfreq"] * 1.5)
    epochs.add_annotations_to_metadata(overwrite=True)
    new_metadata = epochs.metadata
    assert_frame_equal(metadata, new_metadata)


def test_epoch_annotations_cases():
    """Test Epoch Annotations different cases.

    Here, we test the following cases crossed:
    - annotation start is before/after epoch start
    - annotation end is before/after epoch end
    - 1 annotation that is fully outside all epochs (make sure it is dropped)
    - 1 annotation that spans multiple epochs (make sure it shows up in both)

    In addition, tests functionality when Epochs are loaded vs not.
    """
    # set up a test dataset
    epochs, raw, events = _create_epochs_with_annotations()
    epoch_ants = epochs.get_annotations_per_epoch()

    # assert 'no_overlap' is not in any Epoch
    assert all("no_overlap" not in np.array(sublist) for sublist in epoch_ants)

    # assert 'coincident_onset' is not in any Epoch
    assert all("coincident_onset" not in np.array(sublist) for sublist in epoch_ants)

    # all coincident and straddling events should be only in the first Epoch
    first_epoch_ant = np.array(epoch_ants[0])
    assert all(
        x in first_epoch_ant
        for x in [
            "coincident_offset",
            "straddles_onset",
            "straddles_offset",
        ]
    )
    assert all(
        x not in np.array(sublist)
        for sublist in epoch_ants[1:]
        for x in [
            "coincident_offset",
            "straddles_onset",
            "straddles_offset",
        ]
    )

    # 'within_epoch' should be in the second Epoch only
    second_epoch_ant = np.array(epoch_ants[1])
    third_epoch_ant = np.array(epoch_ants[2])
    assert "within_epoch" in second_epoch_ant
    assert "within_epoch" not in first_epoch_ant
    assert all("within_epoch" not in np.array(sublist) for sublist in epoch_ants[2:])

    # 'surround_epoch' should be in the third Epoch only
    assert "surround_epoch" in third_epoch_ant
    assert all("surround_epoch" not in np.array(sublist) for sublist in epoch_ants[:-1])

    # 'multiple' should be in 2nd and 3rd Epoch
    assert "multiple" not in first_epoch_ant
    assert "multiple" in second_epoch_ant
    assert "multiple" in third_epoch_ant

    # if we drop the first Epoch, then some Annotations will now not
    # be part of Epoch Annotations, and others will be shifted
    epochs = Epochs(raw, events=events, tmin=0, tmax=1, baseline=None)
    epochs = epochs.drop(0)
    epoch_ants = epochs.get_annotations_per_epoch()
    assert all(
        x not in np.array(sublist)
        for sublist in epoch_ants
        for x in ["coincident_offset", "straddles_onset", "straddles_offset"]
    )

    # 'multiple' should be in 1st and 2nd Epoch now
    first_epoch_ant = np.array(epoch_ants[0])
    second_epoch_ant = np.array(epoch_ants[1])
    assert "multiple" in first_epoch_ant
    assert "multiple" in second_epoch_ant

    # test that concatenation does not preserve annotations
    old_epochs = epochs.copy()
    with pytest.warns(RuntimeWarning, match="Annotations"):
        concatenate_epochs([epochs, old_epochs])

    # concatenation should not change the *input* Epochs' annotations
    assert epochs.annotations == old_epochs.annotations


@pytest.mark.parametrize("meas_date", (None, (1, 2)))
@pytest.mark.parametrize("first_samp", (0, 10000))
@pytest.mark.parametrize("decim", (1, 2))
def test_epochs_annotations_backwards_compat(
    monkeypatch, tmp_path, meas_date, first_samp, decim
):
    """Test backwards compatibility with Epochs saved without annotations."""

    # loading an earlier saved file should work
    def no_sfreq_write_float(a, b, c):
        if b == FIFF.FIFF_MNE_EPOCHS_RAW_SFREQ:
            return
        return write_float(a, b, c)

    # force 'write_float' to not write raw_sfreq
    monkeypatch.setattr(mne.epochs, "write_float", no_sfreq_write_float)

    # create a test epochs dataset
    sfreq, n_epochs = 10.0, 4
    data = np.linspace(0, 1, n_epochs * int(sfreq))[np.newaxis]
    info = create_info(ch_names=1, ch_types="eeg", sfreq=sfreq)
    with info._unlock():
        info["lowpass"] = 1.0
    raw = RawArray(data, info, first_samp)
    raw.set_meas_date(meas_date)
    # Add a single annotation that occurs between 1<t<2
    annot = Annotations(1.1, 0.8, "1_less_t_less_2")
    raw.set_annotations(annot)
    annot = raw.annotations  # fully adjusted, as per docstring
    events = make_fixed_length_events(raw)
    epochs = Epochs(
        raw,
        events,
        tmin=0,
        tmax=1 - 1.0 / sfreq,
        decim=decim,
        baseline=None,
        preload=True,
    )
    assert len(epochs) == n_epochs

    # save it to disc and reload
    fname = tmp_path / "test_epo.fif"
    epochs.save(fname)
    epochs = read_epochs(fname)
    assert epochs.info["sfreq"] == epochs._raw_sfreq
    assert epochs.info["meas_date"] == raw.info["meas_date"]

    # expose the problem at a low level
    assert_allclose(epochs.info["sfreq"], raw.info["sfreq"] / decim)
    # expose it for the real use case
    lens = [len(ann) for ann in epochs.get_annotations_per_epoch()]
    want_lens = [0] * n_epochs
    # this should always be the case, but it only is when decim == 1!
    if decim == 1:
        want_lens[1] = 1  # the one we inserted
    assert lens == want_lens
    # but in practice, people with old -epo.fif *do not have any annotations
    # saved with them*, so we really only need to warn about a risk of bad
    # annot when using EpochsFIF *and* they do `set_annotations` *and* it's
    # an old-style file. It would be nice if we could only do it if they
    # resampled, but we have no record of this, so to be safe we always warn.
    epochs.set_annotations(None)  # should be okay
    lens = [len(ann) for ann in epochs.get_annotations_per_epoch()]
    assert lens == [0] * n_epochs
    with pytest.warns(RuntimeWarning, match="incorrect results"):
        epochs.set_annotations(annot)
    lens = [len(ann) for ann in epochs.get_annotations_per_epoch()]
    assert lens == want_lens


def test_epochs_saving_with_annotations(tmp_path):
    """Test Epochs save correctly with Annotations."""
    # start testing with a new Epochs created and
    # then test roundtrip IO
    pytest.importorskip("pandas")
    epochs, _, _ = _create_epochs_with_annotations()
    info = epochs.info

    # test what happens when we save to disc and reload
    fname = tmp_path / "test_epo.fif"
    epochs.save(fname)

    loaded_epochs = read_epochs(fname)
    assert epochs._raw_sfreq == loaded_epochs._raw_sfreq

    # if metadata is added already, then an error will be raised
    epochs.add_annotations_to_metadata()
    with pytest.raises(RuntimeError, match="Metadata for Epochs already contains"):
        epochs.add_annotations_to_metadata()
    # no error is raised if overwrite is True
    epochs.add_annotations_to_metadata(overwrite=True)

    # annotations onset and duration might be off due to machine precision
    # from saving to disc
    assert len(epochs.annotations) == len(loaded_epochs.annotations)
    assert_array_almost_equal(epochs.annotations.onset, loaded_epochs.annotations.onset)
    assert_array_almost_equal(
        epochs.annotations.duration, loaded_epochs.annotations.duration
    )
    assert_array_equal(
        epochs.annotations.description, loaded_epochs.annotations.description
    )

    # if we set up EpochsArray and save it, it should have raw_sfreq
    # and annotations even without explicit support
    epoch_size = epochs.get_data().shape
    data = rng.random(epoch_size)
    epochs = EpochsArray(data, info)
    assert epochs._raw_sfreq == info["sfreq"]
    assert epochs.annotations is None

    epochs.save(fname, overwrite=True)
    loaded_epochs = read_epochs(fname)
    assert epochs._raw_sfreq == loaded_epochs._raw_sfreq
    assert loaded_epochs.annotations is None


def _get_empty_parametrize():
    test_methods = {
        "add_reference_channels": {"ref_channels": "EEG 999"},
        "apply_function": {"fun": lambda x: x},
        "apply_hilbert": {},
        "as_type": {},
        "average": {},
        "compute_psd": {},
        "drop_channels": {"ch_names": ["EEG 014"]},
        "filter": {"l_freq": 1, "h_freq": 40},
        "interpolate_bads": {},
        "pick": {"picks": [0]},
        "pick_channels": {"ch_names": ["EEG 014"]},
        "pick_types": {"eeg": True},
        "plot": {},
        "plot_image": {},
        "plot_psd": {},
        "plot_psd_topo": {"tmin": 0.1, "tmax": 0.2},
        "plot_psd_topomap": {},
        "plot_topo_image": {},
        "resample": {"sfreq": 100},
        "reorder_channels": {"ch_names": ["EEG 014"]},
        "savgol_filter": {"h_freq": 40},
        "set_eeg_reference": {},
        "shift_time": {"tshift": 0.1},
        "standard_error": {},
        "to_data_frame": {},
    }
    arg_values = [(k, v) for k, v in test_methods.items()]
    arg_ids = test_methods.keys()
    return {"argnames": "method", "argvalues": arg_values, "ids": arg_ids}


@pytest.mark.parametrize(**_get_empty_parametrize())
def test_empty_error(method, epochs_empty):
    """Test that a RuntimeError is raised when certain methods are called."""
    if method[0] == "to_data_frame":
        pytest.importorskip("pandas")
    with pytest.raises(RuntimeError, match="is empty."):
        getattr(epochs_empty.copy(), method[0])(**method[1])
