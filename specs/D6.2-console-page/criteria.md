# D6.2 acceptance criteria

**DEM-01** A Streamlit page offers the packaged sample attacks in a picker and replays the chosen one through the real guard with no API call; replay is the default tab.
**DEM-02** Live mode asks for the passcode and shows a budget line (what one request is held at and what is left of today's cap); it is off, and says so, unless the passcode and the shared spend store are both configured, and the guard's per-request cap covers the largest live request bound.
**DEM-03** Each query's guard decision is shown beside the audit line it wrote (sequence number and hash), from the same case.
**DEM-04** Deployed to the chosen host with every secret in the host's store (no secret in the repository), and usage statistics off.
**DEM-05** Live mode accepts the visitor's own provider key (OWNER D-37): the passcode is still required and checked before any client is built; the per-request cap and session rate limit still apply; nothing is held against the host's budget; the key reaches only that request's SDK client (never the process environment, a log, the audit lines, the aggregate log or the page) and a rejected key is reported without repeating it; a console with only a passcode serves own-key requests only.
