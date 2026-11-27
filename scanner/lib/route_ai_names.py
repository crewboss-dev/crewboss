"""Group/name metadata for the `app/clients/claude.py` AI functions resolved to a calling ROUTE by
`scanner/discover/routes.py`. Hand-checked against the actual route (one entry per function name),
using the coordinator's mapping: quotes/opportunities/leads -> Sales; cases/kb/worknote -> Service;
invoices/supplier bills -> Billing; devices/alerts -> Monitoring; documents -> Documents and Sign;
admin/command -> Platform. A function reachable from MORE than one route (e.g. `draft_email` from
cases.py AND invoices.py AND leads.py; `classify_line` from cases.py AND quotes.py) is named/grouped
for the route this scanner's shortest-path search actually resolved first (file order) — see
scanner/README.md's "Known gaps" for the full list of multi-caller functions this simplifies.

Shape: func_name -> (group, thing, action, subproject).
"""

ROUTE_AI_NAMES = {
    "analyse_document":         ("Documents and Sign", "Document review", "Analyse with AI", "ERP"),
    "classify_intake":          ("Service", "Enquiry intake", "Classify", "ERP"),
    "classify_line":            ("Billing", "Cost code", "Classify", "ERP"),
    "compose_quote":            ("Sales", "Quote", "Compose with AI", "ERP"),
    "draft_email":              ("Service", "Case email", "Draft", "ERP"),
    "draft_resolution":         ("Service", "Case", "Draft resolution", "ERP"),
    "draft_worknote":           ("Service", "Case worknote", "Draft", "ERP"),
    "elaborate_stages":         ("Sales", "Quote stage", "Elaborate", "ERP"),
    "extract_devices":          ("Monitoring", "Device import", "Extract", "ERP"),
    "extract_invoice":          ("Billing", "Supplier invoice", "Extract", "ERP"),
    "extract_persona":          ("Platform", "Chat persona", "Extract", "AiOS"),
    "extract_quote_lines":      ("Sales", "Incoming quote lines", "Extract", "ERP"),
    "parse_command":            ("Platform", "AI command", "Parse", "ERP"),
    "recommend_alert":          ("Monitoring", "Alert", "Recommend", "ERP"),
    "recommend_case":           ("Service", "Case", "Recommend", "ERP"),
    "recommend_device":         ("Monitoring", "Device", "Recommend", "ERP"),
    "recommend_invoice":        ("Billing", "Invoice", "Recommend", "ERP"),
    "recommend_quote":          ("Sales", "Quote next step", "Recommend", "ERP"),
    "refine_kb":                ("Service", "KB article", "Refine", "ERP"),
    "refine_skill":             ("Email and AiOS", "Mail rule skill", "Refine", "ERP"),
    "suggest":                  ("Monitoring", "Pending match", "Suggest", "ERP"),
    "summarise_lead":           ("Sales", "Lead", "Summarise", "ERP"),
    "update_work_summary":      ("Service", "Case work summary", "Update", "ERP"),
}

# Functions with MORE than one route calling them, for the "Known gaps" note (not consumed by code —
# documents the fact this scanner's "first route wins" simplification is hiding).
MULTI_CALLER_FUNCS = {
    "draft_email": ["app/routes/cases.py", "app/routes/invoices.py", "app/routes/leads.py"],
    "classify_line": ["app/routes/cases.py", "app/routes/quotes.py"],
    "classify_intake": ["app/routes/intake.py (support form)", "app/routes/intake.py (lead form)"],
    "refine_skill": ["app/routes/mailrules.py", "app/routes/planning.py"],
}
