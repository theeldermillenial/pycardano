import copy
from dataclasses import replace

import pytest

from pycardano.address import Address
from pycardano.change import ChangeStrategy, SplitByPolicyChange
from pycardano.hash import ScriptHash
from pycardano.transaction import Asset, AssetName, MultiAsset, Value

ADDR = Address.from_primitive(
    "addr_test1vrm9x2zsux7va6w892g38tvchnzahvcd9tykqf3ygnmwtaqyfg52x"
)
POLICY_A = ScriptHash(b"1" * 28)
POLICY_B = ScriptHash(b"2" * 28)

# min_lovelace_post_alonzo of a plain output whose coin takes four bytes, at the
# fixture's 4,310 lovelace/byte
PLAIN_MIN_ADA = 857_690
# min_lovelace_post_alonzo of an output holding one short-named asset of one policy
ONE_ASSET_MIN_ADA = 1_012_850


def _multi_asset(policy, names, quantity=1):
    return MultiAsset({policy: Asset({AssetName(n): quantity for n in names})})


def _long_names(count):
    return [bytes([i]) * 32 for i in range(count)]


def _two_policies():
    """Two assets of POLICY_A (inserted out of order) and one of POLICY_B."""
    return MultiAsset(
        {
            POLICY_B: Asset({AssetName(b"Beta"): 7}),
            POLICY_A: Asset({AssetName(b"Zed"): 3, AssetName(b"Alpha"): 5}),
        }
    )


def _eight_and_one():
    """Eight assets of POLICY_A and one of POLICY_B."""
    multi_asset = _multi_asset(POLICY_A, [b"T%d" % i for i in range(7, -1, -1)], 2)
    multi_asset += _multi_asset(POLICY_B, [b"Beta"], 9)
    return multi_asset


def _names(multi_asset):
    """The (policy, [names]) pairs of a multi-asset, in iteration order."""
    return [
        (policy.payload, [name.payload for name in assets])
        for policy, assets in multi_asset.items()
    ]


def _total(outputs):
    return sum((o.amount for o in outputs), Value())


def _cbor_len(outputs):
    return sum(len(o.to_cbor()) for o in outputs)


def test_split_is_a_change_strategy():
    assert isinstance(SplitByPolicyChange(), ChangeStrategy)


def test_split_two_policies_plain_first(chain_context):
    change = Value(20_000_000, _two_policies())

    outs = SplitByPolicyChange().change_outputs(change, ADDR, chain_context)

    assert len(outs) == 3
    assert not outs[0].amount.multi_asset
    assert [_names(o.amount.multi_asset) for o in outs[1:]] == [
        [(b"1" * 28, [b"Alpha", b"Zed"])],
        [(b"2" * 28, [b"Beta"])],
    ]
    assert outs[1].amount.multi_asset == MultiAsset(
        {POLICY_A: Asset({AssetName(b"Alpha"): 5, AssetName(b"Zed"): 3})}
    )
    assert outs[2].amount.multi_asset == _multi_asset(POLICY_B, [b"Beta"], 7)
    assert all(o.address == ADDR for o in outs)
    assert all(o.datum is None and o.script is None for o in outs)
    assert _total(outs) == change


def test_split_asset_cap_seven_and_one(chain_context):
    change = Value(30_000_000, _eight_and_one())

    capped = SplitByPolicyChange(max_assets_per_output=7).change_outputs(
        change, ADDR, chain_context
    )
    assert [_names(o.amount.multi_asset) for o in capped[1:]] == [
        [(b"1" * 28, [b"T%d" % i for i in range(7)])],
        [(b"1" * 28, [b"T7"])],
        [(b"2" * 28, [b"Beta"])],
    ]
    assert _total(capped) == change

    per_unit = SplitByPolicyChange(max_assets_per_output=1).change_outputs(
        change, ADDR, chain_context
    )
    assert [_names(o.amount.multi_asset) for o in per_unit[1:]] == [
        [(b"1" * 28, [b"T%d" % i])] for i in range(8)
    ] + [[(b"2" * 28, [b"Beta"])]]
    assert _total(per_unit) == change

    uncapped = SplitByPolicyChange().change_outputs(change, ADDR, chain_context)
    assert len(uncapped) == 3


def test_split_per_output_floor(chain_context):
    # The floor wins over a short-named output's min-ADA.
    change = Value(5_000_000, _multi_asset(POLICY_A, [b"A"]))
    outs = SplitByPolicyChange(min_lovelace_per_output=5_000_000).change_outputs(
        change, ADDR, chain_context
    )
    assert len(outs) == 1
    assert outs[0].amount.coin == 5_000_000

    # One output holding 30 assets with 32-byte names needs more than the floor.
    big = _multi_asset(POLICY_A, _long_names(30))
    split = SplitByPolicyChange(min_lovelace_per_output=5_000_000)
    assert split.min_lovelace(Value(0, big), ADDR, chain_context) == 5_529_730
    outs = split.change_outputs(Value(5_529_730, big), ADDR, chain_context)
    assert [o.amount.coin for o in outs] == [5_529_730]

    # Seven assets with 32-byte names need more than a 2-ADA floor.
    seven = _multi_asset(POLICY_A, _long_names(7))
    split = SplitByPolicyChange(
        max_assets_per_output=7, min_lovelace_per_output=2_000_000
    )
    assert split.min_lovelace(Value(0, seven), ADDR, chain_context) == 2_055_870
    outs = split.change_outputs(Value(2_055_870, seven), ADDR, chain_context)
    assert [o.amount.coin for o in outs] == [2_055_870]


def test_split_fold_threshold(chain_context):
    multi_asset = _multi_asset(POLICY_A, [b"A"])
    split = SplitByPolicyChange(
        min_lovelace_per_output=5_000_000, min_plain_lovelace=6_000_001
    )

    folded = split.change_outputs(Value(11_000_000, multi_asset), ADDR, chain_context)
    assert [o.amount for o in folded] == [Value(11_000_000, multi_asset)]

    plain = split.change_outputs(Value(11_000_001, multi_asset), ADDR, chain_context)
    assert [o.amount for o in plain] == [
        Value(6_000_001),
        Value(5_000_000, multi_asset),
    ]

    # With the default 0, a remainder below the plain output's min-ADA folds.
    default = SplitByPolicyChange()
    coin = ONE_ASSET_MIN_ADA + PLAIN_MIN_ADA - 1
    folded = default.change_outputs(Value(coin, multi_asset), ADDR, chain_context)
    assert [o.amount for o in folded] == [Value(coin, multi_asset)]

    plain = default.change_outputs(Value(coin + 1, multi_asset), ADDR, chain_context)
    assert [o.amount for o in plain] == [
        Value(PLAIN_MIN_ADA),
        Value(ONE_ASSET_MIN_ADA, multi_asset),
    ]

    # No remainder: no plain output.
    exact = default.change_outputs(
        Value(ONE_ASSET_MIN_ADA, multi_asset), ADDR, chain_context
    )
    assert [o.amount for o in exact] == [Value(ONE_ASSET_MIN_ADA, multi_asset)]


def test_split_shortfall_returns_none(chain_context):
    multi_asset = _two_policies()
    split = SplitByPolicyChange(min_lovelace_per_output=5_000_000)
    assert split.min_lovelace(Value(0, multi_asset), ADDR, chain_context) == 10_000_000

    assert (
        split.change_outputs(Value(9_999_999, multi_asset), ADDR, chain_context) is None
    )
    assert split.change_outputs(Value(10_000_000, multi_asset), ADDR, chain_context)


def test_split_ada_only_returns_none(chain_context):
    assert (
        SplitByPolicyChange().change_outputs(Value(10_000_000), ADDR, chain_context)
        is None
    )
    assert (
        SplitByPolicyChange().change_outputs(
            Value(10_000_000, MultiAsset()), ADDR, chain_context
        )
        is None
    )
    assert (
        SplitByPolicyChange().change_outputs(
            Value(10_000_000, MultiAsset({POLICY_A: Asset()})), ADDR, chain_context
        )
        is None
    )


def test_split_value_over_max_val_size_returns_none(chain_context):
    chain_context.protocol_param = replace(
        chain_context.protocol_param, max_val_size=100
    )
    change = Value(20_000_000, _multi_asset(POLICY_A, _long_names(7)))

    assert SplitByPolicyChange().change_outputs(change, ADDR, chain_context) is None
    assert SplitByPolicyChange(max_assets_per_output=1).change_outputs(
        change, ADDR, chain_context
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_assets_per_output": 0},
        {"max_assets_per_output": -1},
        {"min_lovelace_per_output": -1},
        {"min_plain_lovelace": -1},
    ],
)
def test_split_rejects_bad_parameters(kwargs):
    with pytest.raises(ValueError):
        SplitByPolicyChange(**kwargs)


def test_split_keeps_its_parameters():
    split = SplitByPolicyChange(7, 5_000_000, 6_000_001)
    assert split.max_assets_per_output == 7
    assert split.min_lovelace_per_output == 5_000_000
    assert split.min_plain_lovelace == 6_000_001

    default = SplitByPolicyChange()
    assert default.max_assets_per_output is None
    assert default.min_lovelace_per_output == 0
    assert default.min_plain_lovelace == 0


@pytest.mark.parametrize("coin", [0, 1_000, 30_000_000])
def test_split_does_not_mutate_change(chain_context, coin):
    change = Value(coin, _eight_and_one())
    before = copy.deepcopy(change)

    SplitByPolicyChange(max_assets_per_output=7).change_outputs(
        change, ADDR, chain_context
    )
    SplitByPolicyChange().min_lovelace(change, ADDR, chain_context)
    SplitByPolicyChange().group(change.multi_asset)

    assert change == before


def test_split_outputs_do_not_alias_change(chain_context):
    change = Value(30_000_000, _eight_and_one())
    outs = SplitByPolicyChange().change_outputs(change, ADDR, chain_context)

    for output in outs:
        for policy, assets in output.amount.multi_asset.items():
            assert assets is not change.multi_asset[policy]


SWEEP_PARAMS = [
    {},
    {"max_assets_per_output": 7},
    {"max_assets_per_output": 1},
    {"min_lovelace_per_output": 5_000_000},
    {"min_lovelace_per_output": 2_000_000, "max_assets_per_output": 7},
    {"min_plain_lovelace": 6_000_001},
    {
        "max_assets_per_output": 7,
        "min_lovelace_per_output": 5_000_000,
        "min_plain_lovelace": 6_000_001,
    },
]


@pytest.mark.parametrize("params", SWEEP_PARAMS)
def test_split_cbor_length_monotone_in_coin(chain_context, params):
    split = SplitByPolicyChange(**params)
    for multi_asset in (
        _eight_and_one(),
        _multi_asset(POLICY_A, _long_names(30)),
    ):
        floor = split.min_lovelace(Value(0, multi_asset), ADDR, chain_context)
        thresholds = (PLAIN_MIN_ADA, split.min_plain_lovelace, 24, 256, 65_536)
        deltas = {0, 1, 2, 23, 255, 1_000_000, 2**32, 50_000_000}
        for threshold in thresholds:
            deltas |= {threshold - 1, threshold, threshold + 1}
        coins = sorted({floor + d for d in deltas if d >= 0}, reverse=True)

        lengths = []
        for coin in coins:
            outs = split.change_outputs(Value(coin, multi_asset), ADDR, chain_context)
            assert outs is not None
            assert _total(outs) == Value(coin, multi_asset)
            lengths.append(_cbor_len(outs))

        assert lengths == sorted(lengths, reverse=True), (params, coins, lengths)
        assert (
            split.change_outputs(Value(floor - 1, multi_asset), ADDR, chain_context)
            is None
        )


def test_group_canonical_order_and_min_lovelace(chain_context):
    multi_asset = _two_policies()
    split = SplitByPolicyChange()

    groups = split.group(multi_asset)

    assert [_names(g) for g in groups] == [
        [(b"1" * 28, [b"Alpha", b"Zed"])],
        [(b"2" * 28, [b"Beta"])],
    ]
    assert sum(groups, MultiAsset()) == multi_asset
    # the min-ADA of the POLICY_A output plus that of the POLICY_B output
    assert split.min_lovelace(Value(0, multi_asset), ADDR, chain_context) == (
        1_051_640 + 1_025_780
    )
    assert split.min_lovelace(Value(5_000_000), ADDR, chain_context) == 0
    assert split.group(MultiAsset()) == []
    assert split.group(MultiAsset({POLICY_A: Asset()})) == []


def test_group_cuts_runs_by_name(chain_context):
    groups = SplitByPolicyChange(max_assets_per_output=3).group(_eight_and_one())

    assert [_names(g) for g in groups] == [
        [(b"1" * 28, [b"T0", b"T1", b"T2"])],
        [(b"1" * 28, [b"T3", b"T4", b"T5"])],
        [(b"1" * 28, [b"T6", b"T7"])],
        [(b"2" * 28, [b"Beta"])],
    ]
