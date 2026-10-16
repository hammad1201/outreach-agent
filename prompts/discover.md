Find {count} real businesses that fit the segment below and would plausibly want what {business} offers them, as described in the segment. Use web search and web fetch to find them and to confirm each one. Search anywhere on the public web — the places named in the segment are starting points, not limits. Vary your searches between runs so you surface businesses others would miss.

<segment name="{segment}">
{brief}
</segment>

Focus this run on: {focus}

Use your searches well: you have about 20 searches and 25 page fetches. Spend them finding businesses' own websites. Job boards, aggregators and directories that hide who the business is are dead ends: skip them. When a listing or post does name the business, one search for that name usually finds its site.

Requirements for every business you return:
- It is a real, currently operating business with its own website. Give its own domain (e.g. "acme.com"), not a marketplace, directory, booking platform, crowdfunding or social-media URL.
- It is based in one of these countries: {countries}. Give the ISO country code.
- `evidence` quotes or closely paraphrases one specific, checkable fact you actually saw that shows the fit, and `evidence_url` is the page where you saw it. No evidence, no entry.
- `why_fit` says in one or two sentences what {business} could do for them, tied to that evidence.
- Follow any size or type limits in the segment. Unless it says otherwise, skip big enterprises, government bodies and direct competitors of {business}.

Do not return any of these domains, already known: {exclude}

Return JSON only, exactly this shape:
{"companies": [{"name": "...", "domain": "...", "country": "US", "why_fit": "...", "evidence": "...", "evidence_url": "https://..."}]}
If you can't find {count} good ones, return fewer. Quality over count.
