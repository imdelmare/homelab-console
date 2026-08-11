# Third-party notices

This document is an inventory, not a replacement for the license text shipped
by each dependency. Release artifacts must preserve the applicable copyright,
license and attribution notices. Versions are pinned by the Python requirement
files and `apps/web/package-lock.json`; those machine-readable files remain the
authoritative dependency inventory.

## Python application dependencies

The direct Python dependencies use the following licenses:

| Component | License |
| --- | --- |
| FastAPI | MIT |
| Uvicorn | BSD-3-Clause |
| Pydantic Settings | MIT |
| python-dotenv | BSD-3-Clause |
| PyYAML | MIT |
| HTTPX | BSD-3-Clause |
| SQLAlchemy | MIT |
| psycopg / psycopg-binary | LGPL-3.0-only |
| argon2-cffi | MIT |
| Alembic | MIT |
| cryptography | Apache-2.0 OR BSD-3-Clause |
| PyAV | BSD-3-Clause |
| MCP Python SDK | MIT |

Notable transitive packages include `certifi` under MPL-2.0, `greenlet` under
MIT AND PSF-2.0, and the remaining installed application dependencies under
permissive MIT, BSD, Apache, PSF or 0BSD terms.

PyAV binary wheels link to FFmpeg libraries. FFmpeg is normally LGPL-2.1-or-
later, but optional build flags and external libraries can change the effective
license or make a binary non-redistributable. Public images must use a
redistributable FFmpeg/PyAV build and carry its corresponding notices.

## Web application dependencies

The npm lockfile currently contains dependencies under MIT, Apache-2.0, ISC,
BSD-2-Clause, BSD-3-Clause, 0BSD, BlueOak-1.0.0, CC-BY-4.0 and OFL-1.1.
Notable attribution-bearing assets are:

- Inter font files from `@fontsource/inter`, licensed OFL-1.1;
- `caniuse-lite` browser compatibility data, licensed CC-BY-4.0;
- `minimatch`, licensed BlueOak-1.0.0.

The lockfile contains no package with a missing license field at the time this
inventory was generated.

## Runtime services and base images

- PostgreSQL uses the permissive PostgreSQL License.
- The open-source Ollama repository uses the MIT License.
- Caddy uses Apache-2.0.
- Nginx uses BSD-2-Clause.
- Official Python, Node, Debian and Alpine base images contain additional
  operating-system packages whose notices must be retained in distributed
  images.

## Separately licensed components not distributed by this repository

### Gemma model weights

Gemma weights and model derivatives are governed by Google's separate Gemma
terms and prohibited-use policy. They are not part of this source repository or
its release images. Operators install models separately and are responsible for
the notices, restrictions and hosted-service obligations applicable to the
specific Gemma release they use.

### Hosted APIs

OpenAI and Telegram are external hosted services used through documented
network APIs. Their service terms apply independently; their SDKs or services
are not relicensed by this project.
