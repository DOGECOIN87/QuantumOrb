"""Read-only transfer facts and independent balance observations from raw history."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Literal

from app.config import _validate_base58_address
from app.domain.enums import EvidenceState
from app.integrations.wallet_history_models import HistoryTransaction, parse_history_transaction
from app.transfer_context import TransferContext, TransferInstructionContext, parse_transfer_context
from app.transfer_instructions import (
    ASSOCIATED_TOKEN_PROGRAM,
    SYSTEM_PROGRAM,
    TOKEN_2022_PROGRAM,
    TOKEN_PROGRAM,
    DecodedTransferInstruction,
    TransferInstructionError,
    decode_transfer_instruction,
    instruction_bytes,
)

_U64_MAX = 2**64 - 1
_MAX_PAYLOAD = 4 * 1024 * 1024
_DECIMAL = re.compile(r"0|[1-9][0-9]*")
Asset = Literal["sol", "token"]


@dataclass(frozen=True)
class BalanceChange:
    asset: Asset
    account: str
    mint: str | None
    owner: str | None
    program_id: str | None
    pre_amount_raw: str | None
    post_amount_raw: str | None
    delta_raw: str | None
    fee_adjusted_delta_raw: str | None
    decimals: int | None
    state: EvidenceState


@dataclass(frozen=True)
class TokenAccountInfo:
    account: str
    mint: str | None
    owner: str | None
    program_id: str | None
    decimals: int | None
    conflicts: tuple[str, ...] = ()


@dataclass(frozen=True)
class TransferFact:
    fact_key: str
    asset: Asset
    program_id: str
    instruction_name: str
    source_account: str
    destination_account: str
    source_owner: str | None
    destination_owner: str | None
    mint: str | None
    amount_raw: str
    net_amount_raw: str | None
    fee_amount_raw: str | None
    decimals: int | None
    authority: str | None
    top_level_instruction_index: int
    inner_instruction_index: int | None
    stack_height: int | None
    context: Literal["transfer", "account_creation"]
    signature: str
    slot: int
    transaction_index: int
    block_time: int | None
    raw_event_id: int
    page_raw_event_id: int


@dataclass(frozen=True)
class TransferExtraction:
    signature: str | None
    slot: int | None
    transaction_index: int | None
    block_time: int | None
    raw_event_id: int | None
    page_raw_event_id: int | None
    fee_payer: str | None
    signers: tuple[str, ...] | None
    fee_lamports_raw: str | None
    account_keys: tuple[str, ...]
    transfers: tuple[TransferFact, ...]
    native_balance_changes: tuple[BalanceChange, ...]
    token_balance_changes: tuple[BalanceChange, ...]
    token_accounts: tuple[TokenAccountInfo, ...]
    warnings: tuple[str, ...]
    complete: bool
    state: EvidenceState


@dataclass(frozen=True)
class _TokenSnapshot:
    amount: int
    mint: str
    owner: str | None
    program_id: str | None
    decimals: int


def _address(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError
    return _validate_base58_address(value)


def _u64(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= _U64_MAX:
        raise ValueError
    return value


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, value in pairs:
        if key in values:
            raise ValueError
        values[key] = value
    return values


def _constant(value: str) -> Any:
    del value
    raise ValueError


def _finite(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError
    return number


def _native_changes(meta: dict[str, Any], context: TransferContext) -> tuple[BalanceChange, ...]:
    before, after = meta["preBalances"], meta["postBalances"]
    if len(before) != len(context.account_keys) or len(after) != len(before):
        raise ValueError
    fee = _u64(meta["fee"])
    changes = []
    for account, pre, post in zip(context.account_keys, before, after, strict=True):
        pre, post = _u64(pre), _u64(post)
        delta = post - pre
        if delta:
            adjusted = delta + fee if account == context.fee_payer else delta
            changes.append(
                BalanceChange(
                    "sol",
                    account,
                    None,
                    None,
                    None,
                    str(pre),
                    str(post),
                    str(delta),
                    str(adjusted),
                    None,
                    EvidenceState.OBSERVED,
                )
            )
    return tuple(changes)


class TransferExtractor:
    """Decode supported successful transfers; never pair net balance deltas."""

    @staticmethod
    def _failed(history: object) -> TransferExtraction:
        known = history if isinstance(history, HistoryTransaction) else None
        return TransferExtraction(
            signature=known.signature if known else None,
            slot=known.slot if known else None,
            transaction_index=known.transaction_index if known else None,
            block_time=known.block_time if known else None,
            raw_event_id=known.raw_event_id if known else None,
            page_raw_event_id=known.page_raw_event_id if known else None,
            fee_payer=None,
            signers=None,
            fee_lamports_raw=None,
            account_keys=(),
            transfers=(),
            native_balance_changes=(),
            token_balance_changes=(),
            token_accounts=(),
            warnings=("transfer_input_invalid",),
            complete=False,
            state=EvidenceState.ERROR,
        )

    def extract(self, history: HistoryTransaction) -> TransferExtraction:
        """Consume immutable T021 evidence without network or database access."""
        warnings: list[str] = []
        partial = False

        def warn(code: str, *, incomplete: bool = True) -> None:
            nonlocal partial
            if code not in warnings:
                warnings.append(code)
            partial |= incomplete

        try:
            if (
                not isinstance(history, HistoryTransaction)
                or len(history.payload_json.encode()) > _MAX_PAYLOAD
            ):
                raise ValueError
            payload = json.loads(
                history.payload_json,
                object_pairs_hook=_unique,
                parse_constant=_constant,
                parse_float=_finite,
            )
            checked = parse_history_transaction(
                payload,
                raw_event_id=history.raw_event_id,
                page_raw_event_id=history.page_raw_event_id,
            )
            if checked != history:
                raise ValueError
            context = parse_transfer_context(payload)
            meta = payload["meta"]
            native = _native_changes(meta, context)
        except Exception:
            return self._failed(history)
        for warning in context.warnings:
            warn(warning)

        pre = self._token_snapshots(meta.get("preTokenBalances"), context.account_keys, warn)
        post = self._token_snapshots(meta.get("postTokenBalances"), context.account_keys, warn)
        token_info, token_changes = self._token_changes(pre, post, warn)
        if context.transaction_failed:
            warn("failed_transaction", incomplete=False)
            return self._result(
                history,
                context,
                meta,
                (),
                native,
                token_changes,
                token_info,
                warnings,
                not partial,
                EvidenceState.NOT_APPLICABLE,
            )

        associations, changed_owners, changed_mints = self._associations(context, warn)
        if set(context.warnings) & {
            "inner_instruction_context_unavailable",
            "inner_execution_unverified",
            "runtime_log_context_invalid",
        }:
            # Unknown CPI activity may temporarily change ownership or recreate
            # an account even when the final snapshots look identical.
            uncertain = set(token_info) | set(associations)
            changed_owners.update(uncertain)
            changed_mints.update(uncertain)
        for account, association in associations.items():
            observed = token_info.get(account)
            if observed is None:
                token_info[account] = association
            else:
                mint = observed.mint
                owner = observed.owner
                program = observed.program_id
                conflicts = list(observed.conflicts)
                if mint is not None and mint != association.mint:
                    warn("token_mint_conflict")
                    mint = None
                    conflicts.append("mint")
                elif mint is None and "mint" not in conflicts:
                    mint = association.mint
                if owner is not None and owner != association.owner:
                    warn("token_owner_conflict")
                    owner = None
                    conflicts.append("owner")
                elif owner is None and account not in changed_owners and "owner" not in conflicts:
                    owner = association.owner
                if program is not None and program != association.program_id:
                    warn("token_program_conflict")
                    program = None
                    conflicts.append("program")
                elif program is None and "program" not in conflicts:
                    program = association.program_id
                token_info[account] = replace(
                    observed,
                    mint=mint,
                    owner=owner,
                    program_id=program,
                    conflicts=tuple(dict.fromkeys(conflicts)),
                )
        for account in changed_owners:
            if account in token_info:
                token_info[account] = replace(token_info[account], owner=None)
            warn("token_owner_changed")

        facts: list[TransferFact] = []
        for instruction in context.instructions:
            if instruction.execution_status != "succeeded":
                if instruction.program_id in {SYSTEM_PROGRAM, TOKEN_PROGRAM, TOKEN_2022_PROGRAM}:
                    warn("transfer_execution_unverified")
                continue
            try:
                decoded = decode_transfer_instruction(
                    instruction.program_id, instruction.accounts, instruction.data
                )
            except TransferInstructionError as error:
                warn(error.code)
                continue
            if decoded is None:
                continue
            if decoded.source_account == decoded.destination_account:
                warn("self_transfer_ignored", incomplete=False)
                continue
            if decoded.amount_raw == "0":
                warn("zero_transfer_ignored", incomplete=False)
                continue
            if (
                decoded.asset == "token"
                and decoded.mint is None
                and (
                    decoded.source_account in changed_mints
                    or decoded.destination_account in changed_mints
                )
            ):
                warn("transfer_mint_context_changed")
                continue
            fact = self._fact(history, instruction, decoded, token_info, associations, warn)
            if fact is not None:
                facts.append(fact)

        # Native balance residuals are observations, not additional inferred pairs.
        expected: dict[str, int] = {context.fee_payer: -_u64(meta["fee"])}
        for fact in facts:
            if fact.asset == "sol":
                amount = int(fact.amount_raw)
                expected[fact.source_account] = expected.get(fact.source_account, 0) - amount
                expected[fact.destination_account] = (
                    expected.get(fact.destination_account, 0) + amount
                )
        actual = {change.account: int(change.delta_raw or "0") for change in native}
        if any(actual.get(key, 0) != expected.get(key, 0) for key in set(actual) | set(expected)):
            warn("unattributed_native_balance_change")
        return self._result(
            history,
            context,
            meta,
            tuple(facts),
            native,
            token_changes,
            token_info,
            warnings,
            not partial,
            EvidenceState.OBSERVED,
        )

    @staticmethod
    def _token_snapshots(
        rows: Any,
        keys: tuple[str, ...],
        warn: Callable[[str], None],
    ) -> dict[str, _TokenSnapshot]:
        if not isinstance(rows, list) or len(rows) > 256:
            warn("token_balances_unavailable")
            return {}
        snapshots: dict[str, _TokenSnapshot] = {}
        invalid: set[str] = set()
        for row in rows:
            account: str | None = None
            try:
                if not isinstance(row, dict):
                    raise ValueError
                index = row["accountIndex"]
                if type(index) is not int or not 0 <= index < len(keys):
                    raise ValueError
                account = keys[index]
                if account in snapshots or account in invalid:
                    raise ValueError
                mint = _address(row["mint"])
                owner = None if row.get("owner") is None else _address(row["owner"])
                program = None if row.get("programId") is None else _address(row["programId"])
                amounts = row["uiTokenAmount"]
                raw = amounts["amount"]
                decimals = amounts["decimals"]
                if (
                    not isinstance(raw, str)
                    or _DECIMAL.fullmatch(raw) is None
                    or len(raw) > 20
                    or int(raw) > _U64_MAX
                    or type(decimals) is not int
                    or not 0 <= decimals <= 255
                ):
                    raise ValueError
                snapshots[account] = _TokenSnapshot(int(raw), mint, owner, program, decimals)
            except (KeyError, TypeError, ValueError):
                warn("token_balance_invalid")
                if account is not None:
                    invalid.add(account)
                    snapshots.pop(account, None)
        return snapshots

    @staticmethod
    def _token_changes(
        pre: dict[str, _TokenSnapshot],
        post: dict[str, _TokenSnapshot],
        warn: Callable[[str], None],
    ) -> tuple[dict[str, TokenAccountInfo], tuple[BalanceChange, ...]]:
        info: dict[str, TokenAccountInfo] = {}
        changes = []
        for account in sorted(set(pre) | set(post)):
            before, after = pre.get(account), post.get(account)
            present = before or after
            if present is None:
                continue
            mint: str | None = present.mint
            owner: str | None = present.owner
            program: str | None = present.program_id
            decimals: int | None = present.decimals
            conflicts: list[str] = []
            comparable = before is not None and after is not None
            if comparable:
                assert before is not None and after is not None
                if before.mint != after.mint:
                    warn("token_mint_conflict")
                    mint = None
                    conflicts.append("mint")
                    comparable = False
                if before.decimals != after.decimals:
                    warn("token_decimals_conflict")
                    decimals = None
                    conflicts.append("decimals")
                if before.owner is None or after.owner is None:
                    warn("token_owner_unavailable")
                    owner = None
                elif before.owner != after.owner:
                    warn("token_owner_changed")
                    owner = None
                    conflicts.append("owner")
                else:
                    owner = before.owner or after.owner
                if (
                    before.program_id is not None
                    and after.program_id is not None
                    and before.program_id != after.program_id
                ):
                    warn("token_program_conflict")
                    program = None
                    conflicts.append("program")
                else:
                    program = before.program_id or after.program_id
            else:
                warn("token_balance_side_unavailable")
            info[account] = TokenAccountInfo(
                account, mint, owner, program, decimals, tuple(conflicts)
            )
            delta = after.amount - before.amount if comparable and before and after else None
            if delta or delta is None or before != after:
                changes.append(
                    BalanceChange(
                        "token",
                        account,
                        mint,
                        owner,
                        program,
                        str(before.amount) if before else None,
                        str(after.amount) if after else None,
                        str(delta) if delta is not None else None,
                        None,
                        decimals,
                        EvidenceState.OBSERVED if delta is not None else EvidenceState.NOT_READ,
                    )
                )
        return info, tuple(changes)

    @staticmethod
    def _associations(
        context: TransferContext,
        warn: Callable[[str], None],
    ) -> tuple[dict[str, TokenAccountInfo], set[str], set[str]]:
        associations: dict[str, TokenAccountInfo] = {}
        changed: set[str] = set()
        changed_mints: set[str] = set()
        conflicts: set[str] = set()
        for instruction in context.instructions:
            if instruction.execution_status == "failed":
                continue
            if instruction.program_id not in {
                ASSOCIATED_TOKEN_PROGRAM,
                TOKEN_PROGRAM,
                TOKEN_2022_PROGRAM,
            }:
                continue
            try:
                data = instruction_bytes(instruction.data)
                if instruction.program_id in {TOKEN_PROGRAM, TOKEN_2022_PROGRAM}:
                    if data and data[0] == 6 and instruction.accounts:
                        changed.add(instruction.accounts[0])
                    if data and data[0] in {1, 9, 16, 18} and instruction.accounts:
                        changed.add(instruction.accounts[0])
                        changed_mints.add(instruction.accounts[0])
                    continue
                if instruction.execution_status != "succeeded":
                    continue
                if data not in (b"", b"\0", b"\1") or len(instruction.accounts) < 6:
                    warn("associated_token_context_unavailable")
                    continue
                if instruction.accounts[4] != SYSTEM_PROGRAM or instruction.accounts[5] not in {
                    TOKEN_PROGRAM,
                    TOKEN_2022_PROGRAM,
                }:
                    warn("associated_token_context_unavailable")
                    continue
                account = instruction.accounts[1]
                association = TokenAccountInfo(
                    account,
                    instruction.accounts[3],
                    instruction.accounts[2],
                    instruction.accounts[5],
                    None,
                )
                if account in conflicts:
                    continue
                if account in associations and associations[account] != association:
                    associations.pop(account)
                    conflicts.add(account)
                    warn("associated_token_context_conflict")
                else:
                    associations[account] = association
            except TransferInstructionError:
                warn("associated_token_context_unavailable")
        return associations, changed, changed_mints

    @staticmethod
    def _fact(
        history: HistoryTransaction,
        context: TransferInstructionContext,
        decoded: DecodedTransferInstruction,
        accounts: dict[str, TokenAccountInfo],
        associations: dict[str, TokenAccountInfo],
        warn: Callable[[str], None],
    ) -> TransferFact | None:
        source_owner: str | None = None
        destination_owner: str | None = None
        mint, decimals = decoded.mint, decoded.decimals
        if decoded.asset == "token":
            source = accounts.get(decoded.source_account)
            destination = accounts.get(decoded.destination_account)
            known = [item for item in (source, destination) if item is not None]
            if any(set(item.conflicts) & {"mint", "program", "decimals"} for item in known):
                warn("transfer_token_context_conflict")
                return None
            mints = {item.mint for item in known if item.mint is not None}
            if mint is not None:
                mints.add(mint)
            if len(mints) != 1:
                warn("transfer_mint_unavailable" if not mints else "token_mint_conflict")
                return None
            mint = next(iter(mints))
            if any(
                item.program_id is not None and item.program_id != decoded.program_id
                for item in known
            ):
                warn("token_program_conflict")
                return None
            known_decimals = {item.decimals for item in known if item.decimals is not None}
            if decimals is not None:
                known_decimals.add(decimals)
            if len(known_decimals) > 1:
                warn("token_decimals_conflict")
                return None
            if known_decimals:
                decimals = next(iter(known_decimals))
            source_owner = source.owner if source else None
            destination_owner = destination.owner if destination else None
            if source_owner is None or destination_owner is None:
                warn("token_owner_unavailable")
            if decoded.net_amount_raw is None:
                warn("token_transfer_net_amount_unavailable")
        category: Literal["transfer", "account_creation"] = "transfer"
        if decoded.asset == "sol" and ASSOCIATED_TOKEN_PROGRAM in context.ancestor_programs:
            if decoded.destination_account not in associations:
                warn("rent_context_unavailable")
                return None
            category = "account_creation"
        identity = [
            "helius",
            history.signature,
            decoded.program_id,
            context.top_level_index,
            context.inner_instruction_index,
            decoded.asset,
            decoded.source_account,
            decoded.destination_account,
            mint,
            "cartographer-0.1.0",
        ]
        key = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
        return TransferFact(
            fact_key=key,
            asset=decoded.asset,
            program_id=decoded.program_id,
            instruction_name=decoded.instruction_name,
            source_account=decoded.source_account,
            destination_account=decoded.destination_account,
            source_owner=source_owner,
            destination_owner=destination_owner,
            mint=mint,
            amount_raw=decoded.amount_raw,
            net_amount_raw=decoded.net_amount_raw,
            fee_amount_raw=decoded.fee_amount_raw,
            decimals=decimals,
            authority=decoded.authority,
            top_level_instruction_index=context.top_level_index,
            inner_instruction_index=context.inner_instruction_index,
            stack_height=context.stack_height,
            context=category,
            signature=history.signature,
            slot=history.slot,
            transaction_index=history.transaction_index,
            block_time=history.block_time,
            raw_event_id=history.raw_event_id,
            page_raw_event_id=history.page_raw_event_id,
        )

    @staticmethod
    def _result(
        history: HistoryTransaction,
        context: TransferContext,
        meta: dict[str, Any],
        facts: tuple[TransferFact, ...],
        native: tuple[BalanceChange, ...],
        token_changes: tuple[BalanceChange, ...],
        accounts: dict[str, TokenAccountInfo],
        warnings: list[str],
        complete: bool,
        state: EvidenceState,
    ) -> TransferExtraction:
        return TransferExtraction(
            signature=history.signature,
            slot=history.slot,
            transaction_index=history.transaction_index,
            block_time=history.block_time,
            raw_event_id=history.raw_event_id,
            page_raw_event_id=history.page_raw_event_id,
            fee_payer=context.fee_payer,
            signers=context.signers,
            fee_lamports_raw=str(meta["fee"]),
            account_keys=context.account_keys,
            transfers=facts,
            native_balance_changes=native,
            token_balance_changes=token_changes,
            token_accounts=tuple(accounts[key] for key in sorted(accounts)),
            warnings=tuple(warnings),
            complete=complete,
            state=state,
        )
