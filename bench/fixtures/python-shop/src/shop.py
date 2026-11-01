import mailer


def refund(x):
    return x - 1


def total():
    return 100


def add_up(xs):
    return sum(xs)


def parse(s):
    if not s:
        raise ValueError("empty")
    return int(s)


def timeout_for(mode):
    t = 30
    return t


def notify(user):
    return mailer.send(user)
