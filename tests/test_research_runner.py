"""Exercise the public notebook runner through the real QC provider boundary."""

from datetime import timedelta

from test_adapter_quantconnect import NOW, A, IndicatorAPI, algorithm

from examples.ema import EMAConfig, EMAScore
from sugarloaf.adapters.quantconnect import QuantBookServices
from sugarloaf.research import run_node


def test_runner_archives_native_indicator_node_without_execution_or_account():
    api, platform = IndicatorAPI(), algorithm()
    services = QuantBookServices(platform, {A: "A"}, api=api)
    config = EMAConfig(fast=2, slow=3, interval=timedelta(minutes=1))
    run = run_node(
        lambda: EMAScore(A, config),
        services,
        start=NOW,
        end=NOW + timedelta(minutes=3),
        configuration={"fast": 2, "slow": 3},
    )
    assert len(run.archive) == 3
    assert [record.batch_sequence for record in run.archive] == [1, 2, 3]
    assert all(record.view.metadata.source == "signal" for record in run.archive)
    assert not hasattr(run.runtime, "execution")
    assert run.provenance["configuration"] == {"fast": 2, "slow": 3}
    assert len(run.provenance["provider_details"]["indicators"]) == 2
