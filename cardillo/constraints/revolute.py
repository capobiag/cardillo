import numpy as np
from cardillo.constraints._base import PositionOrientationBase


class Revolute(PositionOrientationBase):
    def __init__(
        self,
        subsystem1,
        subsystem2,
        axis,
        r_OB0=None,
        A_IB0=None,
        frame_ID1=np.zeros(3),
        frame_ID2=np.zeros(3),
    ):
        self.axis = axis
        self.plane_axes = np.roll([0, 1, 2], -axis)[1:]
        projection_pairs_rotation = [
            (axis, self.plane_axes[0]),
            (axis, self.plane_axes[1]),
        ]

        super().__init__(
            subsystem1,
            subsystem2,
            projection_pairs_rotation=projection_pairs_rotation,
            r_OB0=r_OB0,
            A_IB0=A_IB0,
            frame_ID1=frame_ID1,
            frame_ID2=frame_ID2,
        )

    def assembler_callback(self):
        self.n_full_rotations = 0
        self.previous_quadrant = 1
        super().assembler_callback()

    def _compute_quadrant(self, x, y):
        if x > 0 and y >= 0:
            return 1
        elif x <= 0 and y > 0:
            return 2
        elif x < 0 and y <= 0:
            return 3
        elif x >= 0 and y < 0:
            return 4
        else:
            raise RuntimeError("You should never be here!")

    def angle(self, t, q):
        A_IB1 = self.A_IB1(t, q)
        A_IB2 = self.A_IB2(t, q)

        a, b = self.plane_axes

        e_a1 = A_IB1[:, a]
        e_b1 = A_IB1[:, b]
        e_a2 = A_IB2[:, a]

        # projections
        y = e_a2 @ e_b1
        x = e_a2 @ e_a1

        quadrant = self._compute_quadrant(x, y)

        # check if a full rotation happens
        if self.previous_quadrant == 4 and quadrant == 1:
            self.n_full_rotations += 1
        elif self.previous_quadrant == 1 and quadrant == 4:
            self.n_full_rotations -= 1
        self.previous_quadrant = quadrant

        # compute rotation angle without singularities
        angle = self.n_full_rotations * 2 * np.pi
        if quadrant == 1:
            angle += np.arctan(y / x)
        elif quadrant == 2:
            angle += 0.5 * np.pi + np.arctan(-x / y)
        elif quadrant == 3:
            angle += np.pi + np.arctan(-y / -x)
        else:
            angle += 1.5 * np.pi + np.arctan(x / -y)

        return angle

    def angle_dot(self, t, q, u):
        e_c1 = self.A_IB1(t, q)[:, self.axis]
        return (self.Omega2(t, q, u) - self.Omega1(t, q, u)) @ e_c1

    def angle_dot_q(self, t, q, u):
        e_c1 = self.A_IB1(t, q)[:, self.axis]
        e_c1_q1 = self.A_IB1_q1(t, q)[:, self.axis]

        return np.concatenate(
            [
                (self.Omega2(t, q, u) - self.Omega1(t, q, u)) @ e_c1_q1
                - e_c1 @ self.Omega1_q1(t, q, u),
                e_c1 @ self.Omega2_q2(t, q, u),
            ]
        )

    def angle_dot_u(self, t, q, u):
        e_c1 = self.A_IB1(t, q)[:, self.axis]
        return e_c1 @ np.concatenate([-self.J_R1(t, q), self.J_R2(t, q)], axis=1)

    def angle_q(self, t, q):
        A_IB1 = self.A_IB1(t, q)
        A_IB2 = self.A_IB2(t, q)
        A_IB1_q1 = self.A_IB1_q1(t, q)
        A_IB2_q2 = self.A_IB2_q2(t, q)

        a, b = self.plane_axes

        e_a1 = A_IB1[:, a]
        e_b1 = A_IB1[:, b]
        e_a2 = A_IB2[:, a]

        e_a1_q1 = A_IB1_q1[:, a]
        e_b1_q1 = A_IB1_q1[:, b]
        e_a2_q2 = A_IB2_q2[:, a]

        # projections
        y = e_a2 @ e_b1
        x = e_a2 @ e_a1

        x_q = np.concatenate((e_a2 @ e_a1_q1, e_a1 @ e_a2_q2))
        y_q = np.concatenate((e_a2 @ e_b1_q1, e_b1 @ e_a2_q2))

        return (x * y_q - y * x_q) / (x**2 + y**2)

    def W_angle(self, t, q):
        J_R1 = self.J_R1(t, q)
        J_R2 = self.J_R2(t, q)
        e_c1 = self.A_IB1(t, q)[:, self.axis]
        return np.concatenate([-J_R1.T @ e_c1, J_R2.T @ e_c1])

    def W_angle_q(self, t, q):
        nq1 = self._nq1
        nu1 = self._nu1

        J_R1 = self.J_R1(t, q)
        J_R2 = self.J_R2(t, q)
        J_R1_q1 = self.J_R1_q1(t, q)
        J_R2_q2 = self.J_R2_q2(t, q)

        e_c1 = self.A_IB1(t, q)[:, self.axis]
        e_c1_q1 = self.A_IB1_q1(t, q)[:, self.axis]

        # dense blocks
        dense = np.zeros((self._nu, self._nq))
        dense[:nu1, :nq1] = np.einsum("i,ijk->jk", -e_c1, J_R1_q1) - J_R1.T @ e_c1_q1
        dense[nu1:, :nq1] = J_R2.T @ e_c1_q1
        dense[nu1:, nq1:] = np.einsum("i,ijk->jk", e_c1, J_R2_q2)

        return dense

    def reset(self):
        self.n_full_rotations = 0
        self.previous_quadrant = 1