# Submission checklist (tokens& form) — due 4:30 PM ET

- [ ] **GitHub repo URL:** `https://github.com/<org>/<repo>`
- [ ] **Demo video link:** `https://...` (also paste it into README → Demo video)
- [ ] **What we built** (paste as-is):

  > CarWitness (车证) is "Ctrl+F for street cameras": Clearcam-style detection, search and alerts focused on vehicles, built on VAST DataEngine and NVIDIA Cosmos. A live event feed runs alert rules over the camera archive, and anyone can ask in English or Chinese ("white SUV almost hit a pedestrian at the intersection") to get matching clips with Cosmos3-Reason descriptions, YOLO11 detections and playable video. Selected clips become a bilingual incident report with a closed event vocabulary and an evidence-citation guardrail that drops any unsupported finding. Any clip can also become an AV-simulation corner-case scene card in JSON, for dealers, insurers, fleets and AV teams.

- [ ] **Tools used** (paste as-is):

  > VAST DataEngine, VastDB, VAST S3, NVIDIA Cosmos3-Reason, NVIDIA Cosmos-Embed1, YOLO11, CoreWeave Kubernetes, W&B Serverless Inference, W&B Weave, Cursor, Claude Code

- [ ] **Team names + emails:**
  - _Name_ — _email_
  - _Name_ — _email_
  - _Name_ — _email_
- [ ] **Deployed app screenshot:** open https://workshop.thecosmoslabs.com → **App**. Run one search, generate a report, and screenshot the page with the feed, the evidence cards and the report visible. Save it as `docs/screenshot.png` (optional: embed it in README).

## Before you push: secret scan

```bash
git grep -nIiE "password|secret|api[_-]?key|token" -- . ':!README.md' ':!SUBMISSION.md'
```

How to read the output:

- **Fine:** lines that only mention env var **names** or code identifiers, such as `os.environ.get("WANDB_API_KEY")`, `VSS_PASSWORD`, `self._token`, `secretRef`, `SECRET_NAME`, `printf 'VSS_PASSWORD=%s\n' "$VSS_PASSWORD"` in `deploy.sh`, or `"password": VSS_PASSWORD` in `main.py`.
- **Stop and fix:** any line containing an actual **value**, such as a long random string, `PASSWORD=hunter2`, `WANDB_API_KEY=<40 hex chars>`, a JWT (`eyJ...`) or a kubeconfig `token:` / `client-key-data:`. Remove it, and if it was ever committed, rotate the credential.
- Also confirm that no config or kubeconfig files are tracked. This should print nothing:

  ```bash
  git ls-files | grep -E '\.config$|kubeconfig|-k8s\.yaml$|^\.env'
  ```
