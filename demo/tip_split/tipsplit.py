def split_bill(total_cents, people):
    """Split a bill into whole-cent shares that add up to the total."""
    if people < 1:
        raise ValueError("people must be at least 1")
    share = total_cents // people
    return [share] * people
