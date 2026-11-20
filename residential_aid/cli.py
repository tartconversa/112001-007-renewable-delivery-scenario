"""Command-line interface.

    python -m residential_aid completeness
    python -m residential_aid priority
    python -m residential_aid status
    python -m residential_aid plans
    python -m residential_aid all
    python -m residential_aid --data data approve A02
    python -m residential_aid --data data install A02 2026-07-05
    python -m residential_aid --data data revise-bill E02 2026-01 -120 --reason "..."
    python -m residential_aid --data data delay A03 2026-08-05 --reason "..."
    python -m residential_aid --data data publish 2026-Q3-baseline
    python -m residential_aid --data data impact

Mutating commands print the resulting records and decision log as JSON.  With
``--state FILE`` (or ``--state`` alone for the default ``.state.json`` in the
fixture directory) each command reads and writes a separate JSON session file;
the input CSVs are never modified.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .analysis import Analysis
from .loader import DatasetError
from .registry import RegistrationError
from .state import load_state, save_state


def _json(value) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def _decimal(text: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from exc


def _state_path(args) -> Path:
    if args.state == ".":
        return Path(args.data) / "session.state.json"
    return Path(args.state)


def _analysis(args) -> Analysis:
    analysis, _ = load_state(args.data, _state_path(args))
    return analysis


def _mutating(args) -> Analysis:
    analysis, path = load_state(args.data, _state_path(args))
    args._state_file = path
    return analysis


def _finish(args, analysis: Analysis) -> None:
    if args.state:
        save_state(analysis, args.data, args._state_file)


def cmd_completeness(args) -> int:
    print(_json(_analysis(args).completeness_report()))
    return 0


def cmd_priority(args) -> int:
    print(_json(_analysis(args).priority_report()))
    return 0


def cmd_status(args) -> int:
    print(_json(_analysis(args).status_report()))
    return 0


def cmd_quality(args) -> int:
    print(_json(_analysis(args).data_quality()))
    return 0


def cmd_plans(args) -> int:
    analysis = _analysis(args)
    print(_json([result.summary() for result in analysis.plans()]))
    return 0


def cmd_all(args) -> int:
    analysis = _analysis(args)
    print(
        _json(
            {
                "data_quality": analysis.data_quality(),
                "priority": analysis.priority_report(),
                "completeness": analysis.completeness_report(),
                "status": analysis.status_report(),
                "plans": [result.summary() for result in analysis.plans()],
                "publications": [
                    {
                        "label": p.label,
                        "published_at": p.published_at,
                        "approvals": len(p.approvals),
                        "plan": len(p.plan),
                        "outcomes": len(p.outcomes),
                    }
                    for p in analysis.publications
                ],
                "impact": [
                    {
                        "label": report.label,
                        "approvals_to_review": list(report.approvals_to_review),
                        "plan_slots_to_release": list(report.plan_slots_to_release),
                        "published_effect_to_retract": list(
                            report.published_effect_to_retract
                        ),
                        "records_changed": report.records_changed,
                        "reasons": list(report.reasons),
                    }
                    for report in analysis.impact()
                ],
            }
        )
    )
    return 0


def cmd_approve(args) -> int:
    analysis = _mutating(args)
    application = analysis.approve(args.application_id, approved_by=args.by)
    print(
        _json(
            {
                "application_id": application.application_id,
                "grant_status": application.grant_status,
                "decisions": analysis.decisions[-1],
            }
        )
    )
    _finish(args, analysis)
    return 0


def cmd_install(args) -> int:
    analysis = _mutating(args)
    application = analysis.register_install(
        args.application_id, args.installed_on, recorded_by=args.by
    )
    print(
        _json(
            {
                "application_id": application.application_id,
                "installed_on": application.installed_on.isoformat()
                if application.installed_on
                else None,
                "status": [
                    row
                    for row in analysis.status_report()
                    if row["application_id"] == application.application_id
                ][0],
            }
        )
    )
    _finish(args, analysis)
    return 0


def cmd_revise_bill(args) -> int:
    analysis = _mutating(args)
    bill_id = analysis.revise_bill(
        account_token=args.account_token,
        period=args.period,
        electric_kwh=args.kwh,
        amount_due=args.amount,
        correction_of=args.correction_of,
        reason=args.reason,
    )
    print(
        _json(
            {
                "bill_id": bill_id,
                "issues": [
                    issue.as_dict()
                    for issue in analysis.issues
                    if issue.record in {bill_id, args.correction_of}
                ],
            }
        )
    )
    _finish(args, analysis)
    return 0


def cmd_delay(args) -> int:
    analysis = _mutating(args)
    application = analysis.delay_install(
        args.application_id,
        args.available_to,
        new_available_from=args.available_from,
        reason=args.reason,
    )
    print(
        _json(
            {
                "application_id": application.application_id,
                "available_to": application.available_to.isoformat()
                if application.available_to
                else None,
                "impact": [
                    {
                        "label": report.label,
                        "plan_slots_to_release": list(report.plan_slots_to_release),
                        "reasons": list(report.reasons),
                    }
                    for report in analysis.impact()
                ],
            }
        )
    )
    _finish(args, analysis)
    return 0


def cmd_publish(args) -> int:
    analysis = _mutating(args)
    publication = analysis.publish(args.label)
    print(_json(publication.as_dict()))
    _finish(args, analysis)
    return 0


def cmd_impact(args) -> int:
    analysis = _analysis(args)
    print(
        _json(
            [
                {
                    "label": report.label,
                    "published_at": report.published_at,
                    "approvals_to_review": list(report.approvals_to_review),
                    "plan_slots_to_release": list(report.plan_slots_to_release),
                    "published_effect_to_retract": list(
                        report.published_effect_to_retract
                    ),
                    "records_changed": report.records_changed,
                    "reasons": list(report.reasons),
                }
                for report in analysis.impact(args.label)
            ]
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m residential_aid",
        description="Residential electrification assistance analysis",
    )
    parser.add_argument(
        "--data", dest="data", default="data", help="fixture directory (default: data)"
    )
    parser.add_argument(
        "--state",
        nargs="?",
        const=".",
        default=".",
        help=(
            "JSON session file for staff decisions (default: "
            "<data>/session.state.json; nothing is written by read-only commands)"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("quality", help="data-quality issues and record counts").set_defaults(
        func=cmd_quality
    )
    sub.add_parser("completeness", help="per-household completeness and scope").set_defaults(
        func=cmd_completeness
    )
    sub.add_parser("priority", help="band-level priority groups").set_defaults(
        func=cmd_priority
    )
    sub.add_parser("status", help="grant / installation / measurement states").set_defaults(
        func=cmd_status
    )
    sub.add_parser("plans", help="compare slot and budget arrangements").set_defaults(
        func=cmd_plans
    )
    sub.add_parser("all", help="everything above, as one JSON document").set_defaults(
        func=cmd_all
    )

    approve = sub.add_parser("approve", help="register a grant approval")
    approve.add_argument("application_id")
    approve.add_argument("--by", default="", help="staff identifier")
    approve.set_defaults(func=cmd_approve)

    install = sub.add_parser("install", help="record an installation")
    install.add_argument("application_id")
    install.add_argument("installed_on")
    install.add_argument("--by", default="", help="staff identifier")
    install.set_defaults(func=cmd_install)

    revise = sub.add_parser("revise-bill", help="append a signed bill correction")
    revise.add_argument("account_token")
    revise.add_argument("period")
    revise.add_argument("kwh", type=_decimal, help="signed kWh delta")
    revise.add_argument("--amount", type=_decimal, default=None)
    revise.add_argument("--correction-of", default=None)
    revise.add_argument("--reason", default="")
    revise.set_defaults(func=cmd_revise_bill)

    delay = sub.add_parser("delay", help="move an installation window end date")
    delay.add_argument("application_id")
    delay.add_argument("available_to", nargs="?", default=None)
    delay.add_argument("--available-from", default=None)
    delay.add_argument("--reason", default="")
    delay.set_defaults(func=cmd_delay)

    publish = sub.add_parser("publish", help="snapshot the current decisions")
    publish.add_argument("label")
    publish.set_defaults(func=cmd_publish)

    impact = sub.add_parser("impact", help="what changed since publication")
    impact.add_argument("label", nargs="?", default=None)
    impact.set_defaults(func=cmd_impact)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (DatasetError, RegistrationError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
