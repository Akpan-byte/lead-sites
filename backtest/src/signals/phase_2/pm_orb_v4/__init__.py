from .signal import pm_orb_v4_reentry_signal

# There is intentionally no `pm_orb_v4_signal` alias. Both the backtest engine
# and the paper trader should import the single re-entry function directly;
# registering the same function under two names caused shared-state bugs where
# the non-reentry registration consumed the ORB state and discarded trades.
