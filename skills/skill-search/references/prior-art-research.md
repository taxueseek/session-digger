# Prior-Art Research

Use this method before creating a new skill or materially redesigning an existing one. The goal is to learn from proven work, not to optimize for popularity or produce a stitched-together derivative.

## 1. Built-in discovery

This method is bundled into `skill-search`. Do not check for, download, install, or load another discovery skill.

Use skills.sh, SkillsMP, and GitHub source directly. `npx` may fetch the Skills CLI package into its normal command cache, but it must not create a separate agent skill installation.

### Source roles

| Source | Best use | Metric meaning | Important limitation |
|---|---|---|---|
| skills.sh | popularity anchor and install discovery | installs are ecosystem adoption telemetry | installs are not satisfaction or correctness |
| SkillsMP | broad GitHub coverage, multilingual and occupation discovery | `stars` are repository stars | independent index, duplicates/localizations, approximate totals, may lag GitHub |
| GitHub | canonical source, history, license, code and permissions | repository-native metadata | repository popularity is not skill quality |
| ClawHub | OpenClaw ecosystem coverage | downloads/installs/stars are telemetry | narrower ecosystem; suspicious entries flagged; read source before adoption |
| Local inventory / local-seek | installed-skill reuse and content signals | presence and content hits only | local suggestion, not external evidence |
| Session-digger usage | historical opportunity/gap signals | structured counts only, raw sessions never included | signal, not proof; needs manual review |

Catalog references: [skills.sh](https://skills.sh/), [SkillsMP](https://skillsmp.com/), [SkillsMP API](https://skillsmp.com/docs/api), [ClawHub](https://clawhub.ai/).

Local engines are opt-in and read-only: `--local` scans installed SKILL.md inventories (local_meta) plus content inside skills roots via local-seek (local_body); `--digger` reads session-digger opportunity/gap JSON. They never merge into external families — they are reported as separate `local` and `usage` blocks.

## 2. Search by intent

Turn the requested capability into 2–4 searches that cover:

- the user's outcome, such as `create agent skills`
- the domain plus action, such as `pdf extraction` or `react performance`
- the quality mechanism, such as `skill evaluation`
- an adjacent term when the first search is noisy

Prefer one reproducible multi-channel run (add `--local` when installed skills may inform the design, `--digger` when history may contain prior attempts):

```bash
python3 scripts/research_prior_art.py "<query 1>" "<query 2>" --strict --summary \
  --local --digger --output reports/prior-art-candidates.json
```

The runner keeps catalog metrics separate, merges matching GitHub/skill families, preserves per-query failures, and requires source review before adoption. Its underlying calls are:

```bash
npx --yes skills find "<query>"
python3 scripts/search_skillsmp.py "<query>" --limit 20 --sort stars
python3 scripts/search_github.py "<query>" --limit 10
python3 scripts/search_clawhub.py "<query>" --limit 10
```

Local engines never merge into external families: `--local` reports an inventory/local-seek block, `--digger` a usage block, each with its own status and hits. Their output is a local signal that sharpens the shortlist, not external evidence.

Keep a candidate only when its actual workflow overlaps the requested job. A popular keyword collision is not prior art.

SkillsMP also supports `--sort recent`, `--language`, `--category`, and `--occupation` through the bundled script. Use filters only when they match the target audience; do not narrow away strong cross-language or adjacent-domain candidates prematurely.

The SkillsMP anonymous API allowance is limited. Prefer one focused request per query, stay within returned rate headers, and do not use wildcard searches. An API key is optional; never ask for or store one unless anonymous limits genuinely block authorized work.

The bundled SkillsMP client retries incomplete/chunked reads, timeouts, connection failures, HTTP 408/425/429, and 5xx responses with capped exponential backoff. It does not retry ordinary 4xx request errors. If retries are exhausted, the unified runner preserves the catalog failure as `missing evidence` and can continue non-strict research with the other catalog.

## 3. Build a defensible shortlist

Aim for 2–4 candidates and cover three roles when available:

1. **Popularity anchor**: the most-installed genuinely relevant skill.
2. **Trust anchor**: a first-party, official, curated, or otherwise strongly reputable source.
3. **Complementary specialist**: a candidate that contributes a different useful mechanism, such as evaluation, safety, packaging, or domain depth.

Record these signals separately:

| Signal | What it supports | What it does not prove |
|---|---|---|
| Installs | adoption and discoverability | satisfaction or output quality |
| User ratings/reviews | expressed user sentiment, if the platform actually exposes them | correctness or safety |
| GitHub stars | repository attention | skill-specific quality |
| First-party/curated status | source authority | completeness for this user's workflow |
| Security audits | known automated risk checks | semantic quality or absence of all risk |
| Recent maintenance | current stewardship | backward compatibility |
| License | reuse boundary | technical quality |

`skills.sh` rankings are based on install telemetry. If no rating/review field is available, record `rating evidence unavailable`; never rename another metric to “rating.”

Check for forks or duplicates, stale repositories, unclear licenses, hidden network behavior, broad permissions, and scripts that would execute remote or destructive actions.

### Merge and deduplicate

1. Normalize the GitHub owner, repository, branch-independent skill path, and skill name.
2. Collapse the same repository/name family when results differ only by translated documentation or catalog mirrors; retain language aliases as provenance.
3. Detect forks and copied descriptions before treating them as independent evidence.
4. Preserve source-specific fields: `skills_sh_installs`, `skillsmp_repo_stars`, `skillsmp_language`, `clawhub_downloads`, and observation date.
5. Rank by relevance and role coverage first. Never sum or average cross-catalog popularity fields.

## 4. Inspect without trusting

Read each candidate's `SKILL.md`, then load only the references, scripts, examples, or eval artifacts that explain a relevant mechanism. Prefer source pages or a temporary checkout over globally installing every candidate.

Do not execute third-party scripts, hooks, installers, or generated commands just to understand them. Audit code and permissions first if execution becomes necessary for a real evaluation.

Respect licenses and attribution. Learn principles, sequencing, validation patterns, and failure handling; do not copy long passages or private assets.

## 5. Synthesize with a contribution ledger

Before drafting, write four buckets:

- `keep`: proven mechanisms that fit the user's job unchanged in principle
- `adapt`: useful mechanisms that require skill conventions, different tools, or lighter gates
- `reject`: popular or polished patterns that add risk, bloat, platform lock-in, or do not fit
- `invent`: new connections, scripts, evals, or workflow improvements created for this user's constraints

The final skill must have a clear thesis beyond “combining the best parts.” Start from the target output contract, then select only mechanisms that improve that contract.

## 6. Preserve evidence proportionally

For a Scaffold skill, summarize the shortlist and synthesis in the handoff. For Production or higher, public, or materially researched work, create `reports/prior-art-research.md` with:

```markdown
# Prior-Art Research

- Researched at: YYYY-MM-DD
- Queries: ...
- Catalogs: skills.sh, SkillsMP
- Rating evidence: available / unavailable

| Candidate | Relevance | skills.sh installs | SkillsMP repo stars | Quality/trust evidence | Adopt | Reject | License |
|---|---|---:|---:|---|---|---|---|

## Original contribution
...

## What we learned from each candidate
- Candidate A: concrete mechanism learned and where it appears in the new skill
- Candidate B: concrete mechanism learned and where it appears in the new skill

## Created skill advantages
- Design advantage: source-visible difference tied to the target job
- Validated advantage: difference supported by named eval or runtime evidence
- Hypothesis: promising difference that remains missing evidence

## Missing evidence
...
```

Date all mutable metrics and link to their sources. A report is evidence of research, not proof that the resulting skill is better; demonstrate improvement through trigger cases, output evals, before/after comparisons, or human review when justified.

The final user-facing response must not merely say “researched several skills.” Name the shortlisted skills, state what was learned from each, explain what was deliberately rejected, and distinguish design advantages from validated outcomes. Use [Creation Handoff](creation-handoff.md) for the final structure.

## 7. Degrade safely

Skip or narrow external discovery when:

- the user explicitly forbids it
- network access is unavailable
- a catalog is rate-limited or temporarily unavailable
- search terms would disclose private data
- the change is purely mechanical and cannot affect behavior

Use the other catalog, direct GitHub search, installed/local skills, and official specifications as fallbacks. Record which catalog was unavailable; mark unavailable comparisons, ratings, or source verification as `missing evidence` and continue with an appropriately modest claim.
