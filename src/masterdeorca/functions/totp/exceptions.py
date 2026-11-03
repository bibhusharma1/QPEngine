"""TOTP-specific exceptions."""


class TOTPException(Exception):
    pass


class TOTPNotEnabledError(TOTPException):
    pass


class TOTPAlreadyEnabledError(TOTPException):
    pass


class TOTPNotEnrolledError(TOTPException):
    pass


class InvalidTOTPCodeError(TOTPException):
    pass


class TOTPEnrollmentNotConfirmedError(TOTPException):
    pass


class InvalidChallengeTokenError(TOTPException):
    pass


class ExpiredChallengeTokenError(TOTPException):
    pass


class ReusedChallengeTokenError(TOTPException):
    pass


class TOTPReplayError(TOTPException):
    pass


class TOTPCooldownError(TOTPException):
    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Too many attempts. Retry after {retry_after_seconds} seconds.")
