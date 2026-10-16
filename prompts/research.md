Decide whether this business is worth a cold email from {business}, and if it is, pull out what the email will need.

Segment: {segment}
<segment>
{brief}
</segment>
Why it was flagged: {why_fit}
Evidence found during discovery: {evidence} ({evidence_url})

Pages from their own website:
<untrusted>
{site}
</untrusted>

Qualify it (`qualified: true`) only if all of these hold:
- It is a real operating business, and the website matches the evidence above.
- There is a concrete, specific reason {business} would help them — not just "every business could use this".
- It fits the segment, including any "skip" rules in it, and nothing in the business facts rules them out. It is not a direct competitor of {business}, a large enterprise or a government body.

`observation` is the single most important field. It is one sentence, written to them, about something specific and checkable on their site or in the evidence — the kind of detail that proves a real person looked. Good: "Your booking page still asks people to call during opening hours." Bad: "I see you're doing great things." If you can't write a specific observation, set `qualified` to false.

`pitch_angle` is one or two sentences on what {business} could do about it, using only the business facts and the segment.
`summary` is two or three sentences on who they are and what they do.
`decision_maker` is the founder, owner or most senior relevant person named on their site, or "" if none is named. Never guess a name.
`country` is where the business is based, as a two-letter ISO code ("GB" for the UK, "US", "IE", "NZ"...).
`fit_score` is 1–10: how likely they are to need and pay for this in the next six months.
