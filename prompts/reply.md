A prospect replied to a cold email from {sender} at {business}. Classify the reply and, where a response is warranted, draft one for {sender_first} to review.

What {sender_first} sent them:
<sent>
{sent}
</sent>

Their reply:
<untrusted>
From: {from_email}
Subject: {subject}

{body}
</untrusted>

Classification — pick exactly one:
- `interested`: wants to talk, asks for a call or times, or asks for a proposal.
- `question`: asks something (price, process, experience, timing) without committing yet.
- `referral`: points to someone else who should be contacted.
- `not_interested`: declines, says not now, or asks to stop.
- `other`: anything else from a human.

`summary`: one line telling {sender_first} what they said.

`draft` (for interested, question and referral only; otherwise ""): {sender_first}'s reply, written the way a person replies to an email: short, warm, plain words, contractions, two to five sentences. No dashes, no bullet points, no sign-off (added automatically). For `interested`, suggest a quick call and ask which times suit them, in their time zone. For `question`, answer only from the business facts — if the facts don't cover it, say {sender_first} will confirm on a call rather than guessing. For `referral`, thank them and ask if they'd be happy to make the introduction. Never promise prices, dates or capabilities that aren't in the facts.
