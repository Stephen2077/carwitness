# Submission checklist (tokens& form) — due 4:30 PM ET

- [ ] **GitHub repo URL:** `https://github.com/Stephen2077/carwitness`
- [ ] **Demo video link:** `https://...` (also paste it into README → Demo video)
- [ ] **What we built** (paste as-is):

  > CarWitness (车证) is "Ctrl+F for street cameras" for vehicle incidents, built on the VAST + NVIDIA Cosmos video pipeline. An event feed runs alert rules over 10 cameras in New York, San Francisco and Toronto. Anyone can ask in English or Chinese ("白色 SUV 开过有行人的路口") and NVIDIA Nemotron turns the request into a visual search; each matching clip comes with its Cosmos3-Reason description, YOLO11 object counts and a fast 640p preview. Selected clips become a bilingual incident report with a closed event vocabulary, where every finding must cite a real clip and unsupported findings are dropped. Any clip can also be exported as an AV-simulation corner-case scene card in JSON. Where the sample VSS UI returns clips, CarWitness returns a case file that dealers, insurers, fleets and AV teams can act on.

- [ ] **Tools used** (paste as-is):

  > VAST DataEngine, VastDB, VAST S3, NVIDIA Cosmos3-Reason, NVIDIA Cosmos-Embed1, YOLO11, NVIDIA Nemotron-3-Ultra (via W&B Serverless Inference), CoreWeave Kubernetes, ffmpeg, Cursor, Claude Code, OpenAI Codex

- [ ] **Team names + emails:**
  - _Name_ — _email_
  - _Name_ — _email_
  - _Name_ — _email_
- [ ] **Deployed app screenshot:** open https://workshop.thecosmoslabs.com → **App**. Run one search, generate a report, and screenshot the page with the feed, the evidence cards and the report visible. A feed screenshot is already in `docs/screenshot.jpg` and embedded in README.

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
