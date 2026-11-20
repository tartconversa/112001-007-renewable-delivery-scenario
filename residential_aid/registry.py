"""Registration, publication and outcome tracking.

Three states are kept strictly apart and never collapsed into one another:

``grant_status``   -- has aid been approved?
``installed_on``   -- has the equipment physically been installed?
``measured change``-- has the post-installation bill actually moved?

A post-installation reading only counts as a *measured* change when it comes
from a bill period after the installation date and the account can be
attributed.  ``post_kwh`` supplied on the application is treated as a
*projected* figure until such a bill exists.

Publication records capture the data fingerprint and the decision basis at the
moment staff published.  When bills are revised or installations slip, the
analysis reports what needs re-checking and keeps the earlier publication
intact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .bills import BillAttribution
from .model import Application, Dataset, utc_timestamp


GRANT_STATES = ("submitted", "waiting", "approved", "rejected", "withdrawn")
INSTALL_STATES = ("not scheduled", "scheduled", "installed", "failed", "cancelled")
MEASUREMENT_STATES = (
    "not yet measurable",
    "projected only",
    "measured improvement",
    "measured increase",
    "measured unchanged",
    "measured, no baseline",
    "not attributable",
)


class RegistrationError(ValueError):
    pass


@dataclass(frozen=True)
class StatusRecord:
    """One application's grant, installation and measured-result state.

    ``measured_*`` fields are only populated when an attributable bill period
    exists after the installation.  ``reported_post_kwh`` is whatever the
    application form claimed; it is never used to declare an outcome.
    """

    application_id: str
    grant_status: str
    install_status: str
    measurement_status: str
    installed_on: str = ""
    basis: str = ""
    reported_post_kwh: Decimal | None = None
    baseline_kwh: Decimal | None = None
    post_bill_kwh: Decimal | None = None
    change_kwh: Decimal | None = None
    change_pct: Decimal | None = None

    @property
    def three_states(self) -> dict[str, str]:
        return {
            "grant": self.grant_status,
            "installation": self.install_status,
            "measured_result": self.measurement_status,
        }


@dataclass(frozen=True)
class PublishedOutcome:
    """A point-in-time publication that later inputs may invalidate."""

    label: str
    published_at: str
    fingerprint: str
    approvals: tuple[dict, ...]
    plan: tuple[dict, ...]
    outcomes: tuple[dict, ...]
    overrides: tuple[dict, ...] = ()

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "published_at": self.published_at,
            "fingerprint": self.fingerprint,
            "approvals": [dict(row) for row in self.approvals],
            "plan": [dict(row) for row in self.plan],
            "outcomes": [dict(row) for row in self.outcomes],
            "overrides": [dict(row) for row in self.overrides],
        }


@dataclass(frozen=True)
class PublicationImpact:
    label: str
    published_at: str
    approvals_to_review: tuple[str, ...] = ()
    plan_slots_to_release: tuple[str, ...] = ()
    published_effect_to_retract: tuple[str, ...] = ()
    records_changed: bool = False
    unchanged: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (
            self.approvals_to_review
            or self.plan_slots_to_release
            or self.published_effect_to_retract
        )


def _install_status(application: Application) -> str:
    if application.installed_on:
        return "installed"
    if application.grant_status == "approved":
        return "scheduled"
    return "not scheduled"


def _period_after(period: str, installed_on: str) -> bool:
    from .model import month_start

    start = month_start(period)
    if start is None:
        return False
    from datetime import date

    try:
        installed = date.fromisoformat(installed_on)
    except ValueError:
        return False
    # A bill period is "after installation" only if it starts after the
    # installation date; the installation month is the baseline month.
    return start > installed


def comparable_bill_kwh(
    dataset: Dataset,
    application: Application,
    attribution: BillAttribution | None,
) -> tuple[Decimal | None, Decimal | None]:
    """Baseline and post-install usage measured from the same share of bills.

    Both figures come from the household's attributed share of whole bills and
    are averaged over their own periods, so a short or long first/last period
    does not masquerade as a saving.  Returns ``(baseline, post)``; either may
    be ``None`` when the data does not support a like-for-like comparison.
    """

    if (
        attribution is None
        or not attribution.attributable
        or attribution.share_factor is None
        or application.installed_on is None
    ):
        return None, None

    from .bills import account_period_kwh

    installed = application.installed_on.isoformat()
    before = [p for p in attribution.periods if not _period_after(p, installed)]
    after = [p for p in attribution.periods if _period_after(p, installed)]

    def mean(periods: list[str]) -> Decimal | None:
        if not periods:
            return None
        total = Decimal(0)
        for period in periods:
            value = account_period_kwh(dataset, attribution.account_token, period)
            if value is None:
                return None
            total += value * attribution.share_factor
        return (total / Decimal(len(periods))).quantize(Decimal("0.01"))

    return mean(before), mean(after)


def _record(
    application: Application,
    install_status: str,
    measurement: str,
    basis: str,
    *,
    installed_on: str = "",
    baseline_kwh: Decimal | None = None,
    post_bill_kwh: Decimal | None = None,
    change_kwh: Decimal | None = None,
    change_pct: Decimal | None = None,
) -> StatusRecord:
    return StatusRecord(
        application_id=application.application_id,
        grant_status=application.grant_status,
        install_status=install_status,
        measurement_status=measurement,
        installed_on=installed_on,
        basis=basis,
        reported_post_kwh=application.post_kwh,
        baseline_kwh=baseline_kwh,
        post_bill_kwh=post_bill_kwh,
        change_kwh=change_kwh,
        change_pct=change_pct,
    )


def status_for(
    dataset: Dataset,
    application: Application,
    attribution: BillAttribution | None,
    comparison: tuple[Decimal | None, Decimal | None] = (None, None),
) -> StatusRecord:
    """Derive the three independent states for one application."""

    install_status = _install_status(application)
    installed = (
        application.installed_on.isoformat() if application.installed_on else ""
    )
    reported = application.post_kwh is not None
    attributable = attribution is not None and attribution.attributable

    if application.installed_on is None:
        if reported:
            return _record(
                application, install_status, "projected only",
                "an expected saving/reading was supplied, but the equipment is not "
                "recorded as installed",
            )
        return _record(
            application, install_status, "not yet measurable",
            "no installation recorded",
        )

    if not attributable:
        return _record(
            application,
            install_status,
            "not attributable",
            (attribution.reason if attribution else "no bill attribution available")
            + "; no household-level usage can be measured",
            installed_on=installed,
        )

    baseline, post = comparison
    if post is None:
        if reported:
            return _record(
                application, install_status, "projected only",
                "a post-install figure was supplied, but no bill period starts after "
                f"{installed}; it is not yet a demonstrated result",
                installed_on=installed,
                baseline_kwh=baseline,
            )
        return _record(
            application, install_status, "not yet measurable",
            f"no bill period starts after {installed}",
            installed_on=installed,
            baseline_kwh=baseline,
        )

    if baseline is None:
        return _record(
            application, install_status, "measured, no baseline",
            "a bill period after installation exists, but no earlier bill period to "
            "compare against",
            installed_on=installed,
            post_bill_kwh=post,
        )

    change = post - baseline
    if change < 0:
        status = "measured improvement"
    elif change > 0:
        status = "measured increase"
    else:
        status = "measured unchanged"
    pct = (change / baseline * Decimal(100)).quantize(Decimal("0.1")) if baseline else None
    return _record(
        application,
        install_status,
        status,
        "mean bill-period usage after installation compared with mean before; "
        "both are the household's attributed share of billed periods",
        installed_on=installed,
        baseline_kwh=baseline,
        post_bill_kwh=post,
        change_kwh=change,
        change_pct=pct,
    )


@dataclass
class Registry:
    """In-memory register of approvals and installations.

    The register only records; it does not infer approval from an installation
    or an installation from an approval.  Registration of a grant requires
    active consent and a priced application.
    """

    grants: dict[str, str] = field(default_factory=dict)
    installations: dict[str, str] = field(default_factory=dict)
    decisions: list[dict] = field(default_factory=list)

    def approve(self, dataset: Dataset, application_id: str, *, approved_by: str = "") -> Application:
        application = dataset.application(application_id)
        if application is None:
            raise RegistrationError(f"unknown application {application_id}")
        if application.quote is None:
            raise RegistrationError(
                f"application {application_id} has no quote and cannot be approved"
            )
        household = dataset.household(application.household_token)
        if household is None:
            raise RegistrationError(
                f"application {application_id} has no matching household"
            )
        if household.consent_status != "active":
            raise RegistrationError(
                f"application {application_id} cannot be approved: consent is "
                f"{household.consent_status or 'blank'}"
            )
        if application.grant_status in {"rejected", "withdrawn"}:
            raise RegistrationError(
                f"application {application_id} is {application.grant_status} and cannot be re-approved"
            )

        self.grants[application_id] = "approved"
        self.decisions.append(
            {
                "at": utc_timestamp(),
                "application_id": application_id,
                "action": "grant approved",
                "from": application.grant_status,
                "to": "approved",
                "approved_by": approved_by,
                "basis": {
                    "quote": application.quote,
                    "measure": application.measure,
                    "provider_id": application.provider_id,
                    "income_band": household.income_band,
                    "occupants": household.occupants,
                },
            }
        )
        return Application(**{**application.__dict__, "grant_status": "approved"})

    def register_install(
        self, dataset: Dataset, application_id: str, installed_on: str, *, recorded_by: str = ""
    ) -> Application:
        application = dataset.application(application_id)
        if application is None:
            raise RegistrationError(f"unknown application {application_id}")
        if application.grant_status != "approved":
            raise RegistrationError(
                f"application {application_id} is {application.grant_status}; "
                "installation cannot substitute for grant approval"
            )
        from datetime import date

        try:
            parsed = date.fromisoformat(installed_on)
        except ValueError as exc:
            raise RegistrationError(
                f"installed_on {installed_on!r} is not an ISO date"
            ) from exc
        if application.available_from and parsed < application.available_from:
            raise RegistrationError(
                f"{installed_on} is before the provider window opens"
            )
        if application.available_to and parsed > application.available_to:
            raise RegistrationError(
                f"{installed_on} is after the provider window closes; "
                "the appointment must be re-booked"
            )
        self.installations[application_id] = installed_on
        self.decisions.append(
            {
                "at": utc_timestamp(),
                "application_id": application_id,
                "action": "installation recorded",
                "installed_on": installed_on,
                "recorded_by": recorded_by,
                "grant_basis": application.grant_status,
            }
        )
        return Application(**{**application.__dict__, "installed_on": parsed})

    def publish(
        self,
        dataset: Dataset,
        *,
        label: str,
        approvals: tuple[dict, ...],
        plan: tuple[dict, ...],
        outcomes: tuple[dict, ...],
        overrides: tuple[dict, ...] = (),
    ) -> PublishedOutcome:
        return PublishedOutcome(
            label=label,
            published_at=utc_timestamp(),
            fingerprint=dataset.fingerprint(),
            approvals=approvals,
            plan=plan,
            outcomes=outcomes,
            overrides=overrides,
        )

    def impact(
        self,
        publication: PublishedOutcome,
        dataset: Dataset,
        statuses: dict[str, StatusRecord],
    ) -> PublicationImpact:
        """What needs revisiting since a publication was made.

        The publication itself is never edited: it is the record of what staff
        decided and announced, with the inputs they had.  This method only
        lists what diverged afterwards.
        """

        changed = publication.fingerprint != dataset.fingerprint()
        approvals_review: list[str] = []
        slots_release: list[str] = []
        retract: list[str] = []
        unchanged: list[str] = []
        reasons: list[str] = []

        for row in publication.approvals:
            application_id = row.get("application_id", "")
            application = dataset.application(application_id)
            if application is None:
                approvals_review.append(application_id)
                reasons.append(f"{application_id}: application is no longer on file")
                continue
            household = dataset.household(application.household_token)
            if household is None or household.consent_status != "active":
                approvals_review.append(application_id)
                reasons.append(
                    f"{application_id}: consent is no longer active; the approval "
                    "basis no longer holds"
                )
            elif application.grant_status != "approved":
                approvals_review.append(application_id)
                reasons.append(
                    f"{application_id}: grant is now {application.grant_status}, "
                    "not the approved status that was published"
                )
            elif row.get("quote") is not None and str(application.quote) != str(
                row.get("quote")
            ):
                approvals_review.append(application_id)
                reasons.append(
                    f"{application_id}: quote changed from {row.get('quote')} to "
                    f"{application.quote}"
                )
            else:
                unchanged.append(application_id)

        for row in publication.plan:
            application_id = row.get("application_id", "")
            application = dataset.application(application_id)
            if application is None:
                slots_release.append(application_id)
                reasons.append(f"{application_id}: planned application is gone")
                continue
            if application.grant_status in {"rejected", "withdrawn"}:
                slots_release.append(application_id)
                reasons.append(f"{application_id}: application is {application.grant_status}")
                continue
            published_end = row.get("available_to")
            current_end = (
                application.available_to.isoformat() if application.available_to else ""
            )
            published_install = row.get("installed_on") or ""
            current_install = (
                application.installed_on.isoformat()
                if application.installed_on
                else ""
            )
            moved = False
            if published_end is not None and str(published_end) != current_end:
                moved = True
                reasons.append(
                    f"{application_id}: provider window moved from "
                    f"{published_end or 'unset'} to {current_end or 'unset'}"
                )
            if published_install != current_install:
                moved = True
                reasons.append(
                    f"{application_id}: installation date changed from "
                    f"{published_install or 'unset'} to {current_install or 'unset'}"
                )
            if moved:
                slots_release.append(application_id)
            elif application.installed_on and application.grant_status != "approved":
                slots_release.append(application_id)
                reasons.append(
                    f"{application_id}: installed without an approved grant"
                )

        for row in publication.outcomes:
            application_id = row.get("application_id", "")
            status = statuses.get(application_id)
            if status is None:
                continue
            published = row.get("measurement_status", "")
            if published and published != status.measurement_status:
                retract.append(application_id)
                reasons.append(
                    f"{application_id}: outcome was published as {published!r} and "
                    f"is now {status.measurement_status!r}; the earlier announcement "
                    "needs correcting"
                )
            else:
                unchanged.append(application_id)

        if changed:
            reasons.append(
                "underlying records changed since publication; the published "
                "approvals, slot plan and outcomes keep the basis they were issued with"
            )

        return PublicationImpact(
            label=publication.label,
            published_at=publication.published_at,
            approvals_to_review=tuple(dict.fromkeys(approvals_review)),
            plan_slots_to_release=tuple(dict.fromkeys(slots_release)),
            published_effect_to_retract=tuple(dict.fromkeys(retract)),
            records_changed=changed,
            unchanged=tuple(dict.fromkeys(unchanged)),
            reasons=tuple(dict.fromkeys(reasons)),
        )
