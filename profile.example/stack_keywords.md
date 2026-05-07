# Stack Keywords
# Format: keyword: points  (matched case-insensitively against full JD text)
# More specific phrases score higher to reward exact-match roles.
# max_score caps the total regardless of how many keywords match.

max_score: 35

# Primary stack — your core differentiating technologies
# [language]: 5
# [framework or platform]: 6

# Secondary stack — technologies you know well but aren't your calling card
# [tool]: 2

# Adjacent — broadly applicable skills that add small signal
# [skill]: 1

## Crawl Config

# Title must contain at least one of these (case-insensitive)
seniority_titles: staff, principal, senior staff, lead engineer, lead developer, architect, tech lead

# Reject title if it contains any of these (case-insensitive). Filters the
# false positives a broad seniority match drags in (e.g. pre-sales architects,
# customer-success leads).
title_exclude: solutions architect, delivery architect, sales engineer, customer success, professional services

# Location must contain at least one of these, or "remote"
location_allow: remote, [country or city], [country or city]

# Tags for RemoteOK API queries
# Use `|` to separate groups. Tags within a group are AND-filtered (RemoteOK
# returns only listings with ALL tags in the group). Each group fires its
# own API call. Group by combos that actually appear together in your work.
# Use `/` for one-or-the-other tags at a position — e.g. `spark/databricks`
# fans out to two queries (one with `spark`, one with `databricks`). Use it
# for technologies that won't co-occur on the same listing.
aggregator_tags: [tag], [tag] | [tag/alt], [tag]

# Keywords for Remotive search
# Use `|` to separate groups. Each group is a full-text search query.
# `/` works the same way — `spark/databricks python` fires two queries.
aggregator_keywords: [keywords] | [keyword/alt] [keyword]

# Minimum stack keyword score to pass pre-filter
min_pre_filter_score: 3
