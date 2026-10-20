# Install and run on Windows (Git Bash)

Needs: Python 3.11–3.13 (python.org installer, "Add to PATH" ticked), Git for Windows (Git Bash). Optional: Node.js
is not needed (the Claude Agent SDK bundles its own CLI). Run every command below in **Git Bash**.

## 1. Unzip and install

```bash
cd ~                                   # or wherever you keep projects
unzip AZRND-AI-SDLC.zip                # creates ./AZRND-AI-SDLC
cd AZRND-AI-SDLC
py -3.12 -m venv .venv                 # or: python -m venv .venv
source .venv/Scripts/activate          # Git Bash on Windows: Scripts/, not bin/
python -m pip install --upgrade pip
pip install -e '.[runtime,agent,studio,dev]'
pkf --help                             # the CLI works
```

Behind a corporate proxy, first: `export REQUESTS_CA_BUNDLE=/c/path/to/corp-ca.pem NODE_EXTRA_CA_CERTS=$REQUESTS_CA_BUNDLE`.

## 2. Credentials for the AI steps and the Studio

```bash
export ANTHROPIC_API_KEY=sk-ant-...    # the AI roles (analyst, decomposer, planner, coder)
export GITHUB_TOKEN=github_pat_...     # Studio only: PRs on your specs repository
```

## 3. Check the install: the test suite (about 15 minutes)

```bash
python -m pytest -q
```

## 4. Build the Entimice pipeline and run it end to end locally

Windows needs a drive in file:// staging URLs, so set staging first. The run uses local stand-ins for Entimice,
the Self-Service API and Vault (`dryrun/entimice_servers.py`) and a moto S3.

```bash
export PKF_STAGING_URL="file:///C:/Users/$USERNAME/pkf-staging"
cd examples
pkf validate
pkf status entimice_api
pkf test pipeline entimice_api
pkf build entimice_api                 # dags/pkf_entimice_api.py (already committed; this rebuilds it)
pkf build --all --check                # the CI gate: dags/ matches the specs
cd ..

# a local S3 (keep this window open, or run it in the background with &)
moto_server -H 127.0.0.1 -p 5000 &

export AWS_ENDPOINT_URL=http://127.0.0.1:5000 AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing \
       AWS_DEFAULT_REGION=eu-north-1
python dryrun/entimice_run.py --out entimice-run    # two runs; summary printed, details in ./entimice-run
```

The stand-ins use ports 18080 (Entimice), 18081 (Self-Service API) and 18200 (Vault): free them if they are taken.

## 5. Start the Studio app

```bash
git clone https://github.com/jsandy16/azrnd-ai-sdlc ~/pkf-specs   # a clone the Studio works in (its specs repository)
pkf studio ~/pkf-specs                                              # opens http://127.0.0.1:8501
```

Then **Setup**: fill in the mode (dev), AWS account and region, the team (your name, e-mail, all four roles, GitHub
login), **Open the team PR**, merge it on GitHub, **Check again**, and choose **Who am I**. See docs/STUDIO.md.
Terraform (≥ 1.10, `terraform.exe` on PATH) is needed only for stage S5/S6 with AWS resources; the Entimice
pipeline is scheduled and needs none.

## Notes

- Airflow itself does not run on Windows; `pkf run` and `dryrun/entimice_run.py` run the same task code locally.
  Deploy `examples/dags/` to your Airflow (Linux) workers with `pip install 'package-factory[runtime]'`.
- Before deploying, replace the local stand-in values in `examples/triggers/entimice_selfservice.yaml`,
  `examples/sources/entimice_studies.yaml` and `examples/destinations/entimice_datalake.yaml` with each environment's
  values (see docs/ENTIMICE_PIPELINE.md).
