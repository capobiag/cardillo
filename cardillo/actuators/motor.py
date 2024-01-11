import numpy as np


class Motor:
    def __init__(self, subsystem, tau):
        if not callable(tau):
            self.tau = lambda t: tau
        else:
            self.tau = tau
        self.nla_tau = 1
        self.ntau = 1

        self.subsystem = subsystem

        self.W_tau = self.subsystem.W_l

    def assembler_callback(self):
        self.qDOF = self.subsystem.qDOF
        self.uDOF = self.subsystem.uDOF

    def la_tau(self, t, q, u):
        return self.tau(t)