"""pkf Studio — a Streamlit front end for package-factory: requirement document → approved, built, locally-run
pipeline, with every stage in git and every approval given on GitHub. See docs/STUDIO_PLAN.md and docs/STUDIO.md."""
VERSION = "0.8.2"
BUILD = "0.8.2"
STUDIO_DIR = ".pkf-studio"
ROLES = ("product_owner", "business_analyst", "architect", "data_engineer")
ROLE_LABELS = {"product_owner": "Product Owner", "business_analyst": "Business Analyst", "architect": "Architect",
               "data_engineer": "Data Engineer"}
MODES = ("dev", "sit", "ppt", "prd")
STRICT_MODES = ("sit", "ppt", "prd")
# Stage PRs may not touch these (they change only through a team PR, code-owner reviewed). The approvals ledger
# (.pkf-studio/approvals/) is written by stage PRs and checked row by row by `pkf studio verify` instead.
PROTECTED_PATHS = (".pkf-studio/project.yaml", ".pkf-studio/team.yaml", ".github/", "tools/", "CODEOWNERS")
