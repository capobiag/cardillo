import numpy as np
from math import cos, sin, pi

import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
import matplotlib.animation as animation

from cardillo.math.algebra import cross3, e1, e2, e3

from cardillo.model import Model
from cardillo.model.rigid_body import Rigid_body2D
from cardillo.model.frame import Frame
from cardillo.model.force import Force
from cardillo.model.contacts import Sphere_to_plane
from cardillo.solver import Scipy_ivp, Euler_backward, Generalized_alpha_1, Moreau, Moreau_sym

from scipy.integrate import solve_ivp

class Ball(Rigid_body2D):
    def __init__(self, m, r, q0=None, u0=None):
        theta = 2 / 5 * m * r**2 
        self.r = r
        super().__init__(m, theta, q0=q0, u0=u0)
        
    def boundary(self, t, q, n=100):
        phi = np.linspace(0, 2 * np.pi, n, endpoint=True)
        K_r_SP = self.r * np.vstack([np.sin(phi), np.cos(phi), np.zeros(n)])
        return np.repeat(self.r_OP(t, q), n).reshape(3, n) + self.A_IK(t, q) @ K_r_SP

if __name__ == "__main__":
    animate = True

    m = 1
    r = 0.1
    g = 9.81
    y0 = 1
    y_dot0 = 0
    phi0 = 0 #np.pi / 4
    phi_dot0 = 10
    r_OS0 = np.array([0, y0, 0])
    # omega0 = np.array([0, 0, 0])
    vS0 = np.array([0, y_dot0, 0]) #+ cross3(omega0, r_OS10)
    q0 = np.array([r_OS0[0], r_OS0[1], phi0])
    u0 = np.array([vS0[0], vS0[1], phi_dot0])
    RB = Ball(m, r, q0, u0)


    frame = Frame(A_IK=np.vstack( (e3, e1, e2) ).T )
    plane = Sphere_to_plane(frame, RB, r, np.array([0.3]), e_N=np.array([0.5]))

    model = Model()
    model.add(RB)
    model.add(Force(lambda t: np.array([0, -g * m, 0]), RB))
    model.add(plane)
    model.assemble()

    t0 = 0
    t1 = 5
    dt = 2.5e-3

    # print(f'gap = {model.g_N(model.t0, model.q0)}')
    # exit()
    # solver = Scipy_ivp(model, t1, dt)
    solver = Moreau(model, t1, dt)
    # solver = Moreau_sym(model, t1, dt)
    # solver = Euler_backward(model, t1, dt, numerical_jacobian=True, debug=True)
    # solver = Euler_backward(model, t1, dt, numerical_jacobian=False, debug=False)
    # solver = Generalized_alpha_1(model, t1, dt, numerical_jacobian=True, debug=True)
    # solver = Generalized_alpha_1(model, t1, dt, t_eval=np.linspace(t0, t1, 100), newton_tol=1.0e-6, numerical_jacobian=False, debug=False)

    sol = solver.solve()
    t = sol.t
    q = sol.q
    u = sol.u
    la_N = sol.la_N

    fig, ax = plt.subplots(3, 1)
    ax[0].set_title('y(t)')
    ax[0].plot(t, q[:, 1], '-k')
    ax[1].set_title('y_dot(t)')
    ax[1].plot(t, u[:, 1], '-k')
    ax[2].set_title('P_N(t)')
    ax[2].plot(t, la_N, '-k')
    plt.show()

    if animate:

           # animate configurations
        fig = plt.figure()
        ax = fig.add_subplot(111)
        
        ax.set_xlabel('x [m]')
        ax.set_ylabel('y [m]')
        ax.axis('equal')
        ax.set_xlim(-2 * y0, 2 * y0)
        ax.set_ylim(-2 * y0, 2 * y0)
        

        # prepare data for animation
        frames = len(t)
        target_frames = min(len(t), 200)
        frac = int(frames / target_frames)
        animation_time = 5
        interval = animation_time * 1000 / target_frames

        frames = target_frames
        t = t[::frac]
        q = q[::frac]

        ax.plot([-2 * y0, 2 * y0], [0, 0], '-k')

        def create(t, q):
            x_S, y_S, _ = RB.r_OP(t, q)
            
            A_IK = RB.A_IK(t, q)
            d1 = A_IK[:, 0] * r
            d2 = A_IK[:, 1] * r
            # d3 = A_IK[:, 2] * r

            COM, = ax.plot([x_S], [y_S], 'ok')
            bdry, = ax.plot([], [],  '-k')
            d1_, = ax.plot([x_S, x_S + d1[0]], [y_S, y_S + d1[1]], '-r')
            d2_, = ax.plot([x_S, x_S + d2[0]], [y_S, y_S + d2[1]], '-g')
            return COM, bdry, d1_, d2_

        COM, bdry, d1_, d2_ = create(0, q[0])

        def update(t, q, COM, bdry,  d1_, d2_):

            x_S, y_S, _ = RB.r_OP(t, q)

            x_bdry, y_bdry, _ = RB.boundary(t, q)
            
            A_IK = RB.A_IK(t, q)
            d1 = A_IK[:, 0] * r
            d2 = A_IK[:, 1] * r
            # d3 = A_IK[:, 2] * r

            COM.set_data([x_S], [y_S])
            bdry.set_data(x_bdry, y_bdry)

            d1_.set_data([x_S, x_S + d1[0]], [y_S, y_S + d1[1]])
            d2_.set_data([x_S, x_S + d2[0]], [y_S, y_S + d2[1]])

            return COM, bdry, d1_, d2_

        def animate(i):
            update(t[i], q[i], COM, bdry, d1_, d2_)

        anim = animation.FuncAnimation(fig, animate, frames=frames, interval=interval, blit=False)
        plt.show()