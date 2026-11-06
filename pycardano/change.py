"""
This module contains strategies that decide how a transaction's change is divided into outputs.
"""

from __future__ import annotations

from typing import List, Optional

from pycardano.address import Address
from pycardano.backend.base import ChainContext
from pycardano.transaction import Asset, MultiAsset, TransactionOutput, Value
from pycardano.utils import min_lovelace_post_alonzo

__all__ = ["ChangeStrategy", "SplitByPolicyChange"]


class ChangeStrategy:
    """Decides how a transaction's change is divided into outputs.

    The base class keeps the builder's default change: ``change_outputs`` returns None.
    """

    def change_outputs(
        self, change: Value, address: Address, context: ChainContext
    ) -> Optional[List[TransactionOutput]]:
        """The change outputs, or None to use the builder's default change.

        Args:
            change (Value): The change to return. Every asset quantity is positive.
                The builder passes a copy.
            address (Address): The change address. Every output pays it, with no
                datum and no script.
            context (ChainContext): Supplies the protocol parameters.

        Returns:
            Optional[List[TransactionOutput]]: Outputs whose amounts sum exactly to
            ``change``, each holding its min-ADA and a value whose CBOR is at most
            ``context.protocol_param.max_val_size``, or None. The builder raises on an
            output that breaks these rules, so return None when a split cannot meet
            them.

        The builder may call this twice in one build, the second time with less ADA
        after it re-estimates the fee. The second result must not be longer in CBOR
        than the first, or the builder uses the default change and logs a WARNING. The
        strategy should be stateless and deterministic: it is deep-copied into the
        draft transaction used to evaluate execution units.
        """
        return None


class SplitByPolicyChange(ChangeStrategy):
    """Splits the change into one plain-ADA output and token outputs grouped by policy.

    Each token output holds the assets of one policy, at most ``max_assets_per_output``
    of them, and a coin of ``min_lovelace_per_output`` or its own min-ADA, whichever is
    higher. The ADA left over goes to one plain-ADA output, placed first. A remainder
    below ``min_plain_lovelace``, or below a plain output's own min-ADA, is added to the
    last token output instead. Policies are ordered by policy id, and the assets of a
    policy by asset name, both as bytes.

    The strategy returns None, and the builder uses its default change, when the change
    holds no assets, when its ADA cannot fund every token output (see
    :meth:`min_lovelace`), or when an output's value would be larger than
    ``max_val_size``.

    The builder ignores the strategy when ``merge_change`` is set. Input selection is
    unchanged: it still reserves ADA for the default change, so a tight selection gets
    the default change. A caller that wants selection to fund the split adds
    :meth:`min_lovelace` to its own selection. When the split change would make the
    transaction larger than ``max_tx_size``, the builder reruns its default change.

    Args:
        max_assets_per_output (Optional[int]): The most assets one token output holds.
            None puts each policy in one output; 1 gives one output per asset.
        min_lovelace_per_output (int): The least coin of each token output. 0 means
            the output's min-ADA.
        min_plain_lovelace (int): The least coin of the plain-ADA output. 0 means the
            output's min-ADA.

    Raises:
        ValueError: When ``max_assets_per_output`` is below 1, or a lovelace parameter
            is negative.
    """

    def __init__(
        self,
        max_assets_per_output: Optional[int] = None,
        min_lovelace_per_output: int = 0,
        min_plain_lovelace: int = 0,
    ):
        if max_assets_per_output is not None and max_assets_per_output < 1:
            raise ValueError(
                f"max_assets_per_output must be at least 1, got {max_assets_per_output}"
            )
        if min_lovelace_per_output < 0:
            raise ValueError(
                "min_lovelace_per_output must not be negative, "
                f"got {min_lovelace_per_output}"
            )
        if min_plain_lovelace < 0:
            raise ValueError(
                f"min_plain_lovelace must not be negative, got {min_plain_lovelace}"
            )
        self.max_assets_per_output = max_assets_per_output
        self.min_lovelace_per_output = min_lovelace_per_output
        self.min_plain_lovelace = min_plain_lovelace

    def change_outputs(
        self, change: Value, address: Address, context: ChainContext
    ) -> Optional[List[TransactionOutput]]:
        """The plain-ADA output, if any, then one output per asset group.

        Args:
            change (Value): The change to return. Every asset quantity is positive.
            address (Address): The change address.
            context (ChainContext): Supplies the protocol parameters.

        Returns:
            Optional[List[TransactionOutput]]: Outputs that sum exactly to ``change``, or
            None when the change holds no assets, its ADA is below
            :meth:`min_lovelace`, or an output's value is larger than ``max_val_size``.
        """
        groups = self.group(change.multi_asset)
        if not groups:
            return None
        coins = [self._token_coin(group, address, context) for group in groups]
        remainder = change.coin - sum(coins)
        if remainder < 0:
            return None

        outputs = []
        if remainder > 0:
            plain = TransactionOutput(address, Value(remainder))
            plain_min = max(
                self.min_plain_lovelace, min_lovelace_post_alonzo(plain, context)
            )
            if remainder >= plain_min:
                outputs.append(plain)
            else:
                coins[-1] += remainder
        outputs += [
            TransactionOutput(address, Value(coin, group))
            for coin, group in zip(coins, groups)
        ]

        max_val_size = context.protocol_param.max_val_size
        if any(len(output.amount.to_cbor()) > max_val_size for output in outputs):
            return None
        return outputs

    def group(self, multi_asset: MultiAsset) -> List[MultiAsset]:
        """The asset groups of the token outputs, in output order.

        Policies are ordered by policy id and, within a policy, assets by name, both as
        bytes. Each policy's assets are cut into consecutive runs of at most
        ``max_assets_per_output``. Each group holds one policy.

        Args:
            multi_asset (MultiAsset): The assets to group.

        Returns:
            List[MultiAsset]: The groups, or an empty list when there are no assets.
        """
        groups = []
        for policy in sorted(multi_asset, key=lambda p: p.payload):
            assets = multi_asset[policy]
            names = sorted(assets, key=lambda n: n.payload)
            run = self.max_assets_per_output or max(len(names), 1)
            for start in range(0, len(names), run):
                chunk = Asset(
                    {name: assets[name] for name in names[start : start + run]}
                )
                groups.append(MultiAsset({policy: chunk}))
        return groups

    def min_lovelace(
        self, change: Value, address: Address, context: ChainContext
    ) -> int:
        """The least ADA the change must hold for the split to apply.

        It is the sum of the token outputs' coins. No plain-ADA output is needed, since
        a remainder too small for one is added to a token output. The builder does not
        call this method; a selection that wants the split funded can.

        Args:
            change (Value): The change. Only its assets are read.
            address (Address): The change address.
            context (ChainContext): Supplies the protocol parameters.

        Returns:
            int: The least coin, in lovelace, or 0 when ``change`` holds no assets.
        """
        return sum(
            self._token_coin(group, address, context)
            for group in self.group(change.multi_asset)
        )

    def _token_coin(
        self, group: MultiAsset, address: Address, context: ChainContext
    ) -> int:
        """The coin of a token output: the per-output floor or its min-ADA."""
        coin = max(self.min_lovelace_per_output, 1_000_000)
        for _ in range(2):
            # A raise of a few bytes' worth of lovelace cannot widen the coin again,
            # so the second probe settles it.
            coin = max(
                self.min_lovelace_per_output,
                min_lovelace_post_alonzo(
                    TransactionOutput(address, Value(coin, group)), context
                ),
            )
        return coin
