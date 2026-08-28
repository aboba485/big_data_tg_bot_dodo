class BotInputError(ValueError):
    pass


class BotAccessError(PermissionError):
    pass


class BotBusyError(RuntimeError):
    pass
