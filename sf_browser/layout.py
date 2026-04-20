"""Layout heuristics for the Keboola-style record detail view.

This module turns a flat list of Salesforce fields into an opinionated page
layout approximating the Lightning UI:

    - ``highlights(...)``        -> top strip of 6-8 key fields (populated).
    - ``sections(...)``          -> ordered list of field groups with titles.
    - ``related_priority(...)``  -> (priority_lists, more_lists) split for
                                    related records.

The heuristics lean on field-name prefixes and types plus a small whitelist
of known "obvious" fields. No YAML overrides yet; those can be added later
without changing the public API.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# Highlights
# --------------------------------------------------------------------------- #

# Fields we promote into the header highlights strip when populated, in
# preference order. First match wins per "slot".
_HIGHLIGHT_CANDIDATES: list[list[str]] = [
    # Identity / classification
    ["Type", "StageName", "Status", "CaseNumber", "OrderNumber"],
    ["Sub_Type__c", "Secondary_Account_Type__c", "Priority"],
    # Owner (resolved to User)
    ["OwnerId"],
    # Business segment
    ["Segment__c", "Segment", "Account_Tier__c", "Tier__c"],
    # Territory / geography
    ["Territory__c", "Region__c", "Theatre__c", "Country", "BillingCountry"],
    # Industry / vertical
    ["Industry", "Primary_Industry__c", "Vertical__c"],
    # Amount (for opportunities / orders)
    ["Amount", "TotalAmount", "Total_Amount__c", "ARR_Amount__c", "AnnualRevenue"],
    # Close / effective date
    ["CloseDate", "EffectiveDate", "EndDate"],
]

_HIGHLIGHT_MAX = 6


def highlights(
    fields: list[dict[str, Any]],
    record: dict[str, Any],
    *,
    max_items: int = _HIGHLIGHT_MAX,
) -> list[dict[str, Any]]:
    """Return up to ``max_items`` populated fields suitable for a highlights strip."""
    field_map = {f["field_name"]: f for f in fields}
    chosen: list[dict[str, Any]] = []
    seen: set[str] = set()

    for slot in _HIGHLIGHT_CANDIDATES:
        for name in slot:
            if name in seen or name not in field_map:
                continue
            val = record.get(name)
            if val in (None, ""):
                continue
            chosen.append(field_map[name])
            seen.add(name)
            break
        if len(chosen) >= max_items:
            break

    return chosen[:max_items]


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #

# Hidden from the Keboola view entirely (still visible in raw view).
_SYSTEM_HIDE = {
    "IsDeleted", "SystemModstamp", "LastViewedDate", "LastReferencedDate",
    "LastActivityDate", "MasterRecordId",
    "BillingLatitude", "BillingLongitude", "BillingGeocodeAccuracy",
    "ShippingLatitude", "ShippingLongitude", "ShippingGeocodeAccuracy",
    "JigsawCompanyId", "Jigsaw", "CleanStatus", "PhotoUrl",
    # Binary placeholder fields — just SF API URLs, not useful to display.
    # The actual binary is served via the Files panel.
    "Body", "VersionData",
}

# Fields that get their own prominent rendering in the header/sidebar
# (we don't want to repeat them in the main detail grid).
_PROMOTED_ELSEWHERE = {
    "Id", "Name", "Subject",
    # Highlights
    "Type", "StageName", "Status", "OwnerId",
    "Segment__c", "Segment", "Territory__c", "Industry",
}

# Section prefix rules — ordered. First matching rule wins.
# Each rule: (title, predicate) where predicate returns True for field_name.
_SECTION_RULES: list[tuple[str, Any]] = [
    ("Company",        re.compile(r"^(Description|Website|Phone|Additional_Domains?__c|Company_.*|Number_of_Employees|NumberOfEmployees|Industry|Primary_Industry|Vertical|Annual_?Revenue|Founded|Account_Source|Marketplace|Parent.*)$", re.I)),
    ("Financials",     re.compile(r"^(Active_Order_ARR|Current_MRR|Amount|TotalAmount|Total_Amount|ARR_.*|MRR_.*|Revenue.*|Invoicing_Currency|CurrencyIsoCode|ICO|TAX_.*|VAT_.*)", re.I)),
    ("Invoicing",      re.compile(r"^(Keboola_.*(Account_Number|SWIFT|IBAN)|Bank_.*|IBAN|SWIFT|Company_Registration|Routing_Code|Bank_Account|Default_Order_Approver|Invoicing_.*|Don_t_Send_.*|Collection_.*|Credit_Limit.*|Payment_Terms.*)", re.I)),
    ("Billing address", re.compile(r"^(Billing|Shipping).*$")),
    ("Contact info",   re.compile(r"^(Email|MobilePhone|Fax|Phone|Other.*Phone|HomePhone|AssistantPhone|AssistantName|Salutation|FirstName|LastName|MiddleName|Suffix|Title|Department|MailingAddress|Mailing_.*|Other_.*)$", re.I)),
    ("Social",         re.compile(r"^(LinkedIn.*|Twitter.*|Facebook.*|Outreach.*|GitHub.*|Google_drive.*|Slack.*)$", re.I)),
    ("Referral",       re.compile(r"^Referr(al|er)_.*$", re.I)),
    ("Partnership",    re.compile(r"^(Partner|Mutual_NDA|Partner_Agreeement).*$", re.I)),
    ("Customer success", re.compile(r"^(CSM_.*|CS_.*|Jumpstart.*|Rating|Decision_making.*|Competitor.*|Subscription.*|Corporate_Goals|Data_Challenges|Other_Technologies|Technologies|SLA|Zendesk_.*|SWOT|Account_Potential.*|Account_Sales_.*|Fiscal_Year_.*|Budgeting.*)$", re.I)),
    ("Telemetry",      re.compile(r"^(Telemetry_.*|CRM_ID.*|Activity_Center.*|Credits_Rollover.*|PPU_Rollover.*|Keboola_Organization.*|Keboola_Maintainer.*|Keboola_Cluster.*|Count_of_Open_Case.*|Gong_Count.*)", re.I)),
    ("Sales insights", re.compile(r"^(Intent_.*|G2_.*|Leady.*|ABM|Account_Score|Growing_Employees|Physical_Location|Funding|Location_Phones|HQ_Location_Phones|Locations|Headcount|NAICS|ISIC|SIC_Code|Keep_in_Touch.*)$", re.I)),
    ("Audit",          re.compile(r"^(CreatedBy.*|CreatedDate|LastModifiedBy.*|LastModifiedDate|OwnerId|RecordTypeId|IsDeleted|SystemModstamp|LastActivityDate|MasterRecordId)$")),
]

_DEFAULT_SECTION_TITLE = "Other fields"


def _classify_field(field: dict[str, Any]) -> str:
    """Return a section title for a given field (by name)."""
    name = field["field_name"]
    if name in _SYSTEM_HIDE:
        return "Audit"  # hidden in Keboola view unless user expands
    for title, rule in _SECTION_RULES:
        if rule.search(name):
            return title
    return _DEFAULT_SECTION_TITLE


def sections(
    fields: list[dict[str, Any]],
    record: dict[str, Any],
) -> list[dict[str, Any]]:
    """Group fields into ordered, named sections — including empty ones.

    Returns a list of ``{title, fields, populated_count, total_count,
    has_populated}``. Each section contains *all* fields that match its
    classification rule, so the user sees the full shape of a record
    (empty fields render dim, just like in Salesforce).

    Rules:
    - Fields promoted into the header (Name, Owner, Type, ...) are excluded.
    - System fields (``_SYSTEM_HIDE``) are excluded from the default sections
      — the template can still show them through ``hidden_system_fields()``.
    - "Other fields" section only includes *populated* fields. Empty unknown
      fields stay hidden to avoid noise.
    - Sections with zero fields are dropped.
    """
    section_order = [title for title, _ in _SECTION_RULES] + [_DEFAULT_SECTION_TITLE]
    buckets: dict[str, list[dict[str, Any]]] = {title: [] for title in section_order}

    for f in fields:
        name = f["field_name"]
        if name in _PROMOTED_ELSEWHERE:
            continue
        if name in _SYSTEM_HIDE:
            continue
        title = _classify_field(f)
        if title == _DEFAULT_SECTION_TITLE:
            # Noise reduction — do not include empties in the catch-all bucket.
            val = record.get(name)
            if val is None or val == "":
                continue
        buckets[title].append(f)

    ordered: list[dict[str, Any]] = []
    for title in section_order:
        items = buckets.get(title) or []
        if not items:
            continue
        populated = sum(
            1 for f in items if record.get(f["field_name"]) not in (None, "")
        )
        ordered.append(
            {
                "title": title,
                "fields": items,
                "populated_count": populated,
                "total_count": len(items),
                "has_populated": populated > 0,
            }
        )
    return ordered


def hidden_system_fields(
    fields: list[dict[str, Any]], record: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return populated system/audit fields we hid from the main view."""
    out: list[dict[str, Any]] = []
    for f in fields:
        if f["field_name"] not in _SYSTEM_HIDE:
            continue
        val = record.get(f["field_name"])
        if val in (None, ""):
            continue
        out.append(f)
    return out


# --------------------------------------------------------------------------- #
# Related records
# --------------------------------------------------------------------------- #

# Per-object priority order for related lists. Order matters.
# Fallback: any object not in the list is "rest".
_RELATED_PRIORITY: dict[str, list[str]] = {
    "Account":      ["Contact", "Opportunity", "Order", "Case", "Quote", "Contract"],
    "Contact":      ["Opportunity", "Case", "Task", "Event", "EmailMessage"],
    "Opportunity":  ["OpportunityLineItem", "OpportunityContactRole", "Quote", "Order", "Task", "Event"],
    "Order":        ["OrderItem", "Invoice__c", "Task"],
    "Contract":     ["Order", "Invoice__c"],
    "Quote":        ["QuoteLineItem", "Order"],
    "Case":         ["EmailMessage", "Task", "CaseComment"],
    "Lead":         ["Task", "Event", "EmailMessage", "CampaignMember"],
    "Campaign":     ["CampaignMember", "Opportunity", "Lead"],
    "Product2":     ["OpportunityLineItem", "OrderItem", "PricebookEntry"],
    "User":         ["Account", "Opportunity", "Task", "Event"],
}

# Custom columns to show per related-list child object (overrides the
# generic Id/Name/field view). These are the columns rendered in Keboola
# view; raw view keeps full columns.
_RELATED_COLUMNS: dict[str, list[str]] = {
    "Contact":               ["Name", "Title", "Email", "Phone"],
    "Opportunity":           ["Name", "StageName", "Amount", "CloseDate", "OwnerId"],
    "Order":                 ["OrderNumber", "Status", "EffectiveDate", "TotalAmount"],
    "OrderItem":             ["Product2Id", "Quantity", "UnitPrice", "TotalPrice"],
    "OpportunityLineItem":   ["Product2Id", "Quantity", "UnitPrice", "TotalPrice"],
    "OpportunityContactRole": ["ContactId", "Role", "IsPrimary"],
    "Quote":                 ["Name", "Status", "TotalPrice", "ExpirationDate"],
    "QuoteLineItem":         ["Product2Id", "Quantity", "UnitPrice", "TotalPrice"],
    "Case":                  ["CaseNumber", "Subject", "Status", "Priority", "OwnerId"],
    "CaseComment":           ["CommentBody", "CreatedById", "CreatedDate"],
    "Contract":              ["ContractNumber", "Status", "StartDate", "EndDate"],
    "Invoice__c":            ["Name", "Status__c", "Total__c", "EffectiveDate__c"],
    "Task":                  ["Subject", "Status", "ActivityDate", "OwnerId"],
    "Event":                 ["Subject", "ActivityDate", "DurationInMinutes", "OwnerId"],
    "EmailMessage":          ["Subject", "FromAddress", "ToAddress", "MessageDate"],
    "CampaignMember":        ["ContactId", "LeadId", "Status"],
    "PricebookEntry":        ["Pricebook2Id", "UnitPrice", "IsActive"],
}


def related_priority(
    parent_object: str,
    related: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split ``related`` lists into (priority, rest) based on parent object.

    The priority list is ordered per ``_RELATED_PRIORITY[parent_object]``.
    Rest keeps the original order and is meant for a "More related" section.
    """
    order = _RELATED_PRIORITY.get(parent_object, [])
    if not order:
        return [], list(related)

    by_object: dict[str, dict[str, Any]] = {r["object"]: r for r in related}
    priority: list[dict[str, Any]] = []
    for obj in order:
        if obj in by_object:
            priority.append(by_object.pop(obj))
    rest = list(by_object.values())
    return priority, rest


def related_columns(child_object: str, available_cols: Iterable[str]) -> list[str]:
    """Columns to display for a related-list preview of ``child_object``.

    Always prepends ``Id``. Falls back to first few available columns when
    no per-object override is defined. Unknown columns are filtered out.
    """
    available = set(available_cols)
    cols: list[str] = []
    if "Id" in available:
        cols.append("Id")

    override = _RELATED_COLUMNS.get(child_object)
    if override:
        cols.extend(c for c in override if c in available and c not in cols)
        if len(cols) > 1:
            return cols

    # Fallback: Name/Subject, then first few string columns.
    for name in ("Name", "Subject", "Title", "CaseNumber", "OrderNumber"):
        if name in available and name not in cols:
            cols.append(name)
            break
    return cols[:6]


# --------------------------------------------------------------------------- #
# Record title
# --------------------------------------------------------------------------- #


def record_title(record: dict[str, Any], obj_name: str) -> str:
    """Best human-readable title for a record."""
    for key in ("Name", "Subject", "Title", "CaseNumber", "OrderNumber"):
        val = record.get(key)
        if val:
            return str(val)
    return record.get("Id") or obj_name
