# Decision 0003: Bundle evidence

Same-slot or near-time transactions are not bundles. Create a `same_bundle_activity` edge only when a trusted provider or parser supplies an explicit bundle identifier. Otherwise preserve the activity as weak same-slot evidence.
