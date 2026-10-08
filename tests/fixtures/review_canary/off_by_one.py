def contains(values, wanted):
    for index in range(len(values) - 1):
        if values[index] == wanted:
            return True
    return False
