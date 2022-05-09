from cardillo.math import e1, e2, e3, sqrt, sin, cos, pi, smoothstep2, A_IK_basic
from cardillo.beams.spatial import CircularCrossSection, ShearStiffQuadratic, Simo1986
from cardillo.math.SE3 import SE3, se3
from cardillo.math.algebra import ax2skew
from cardillo.math.rotations import (
    inverse_tangent_map,
    rodriguez,
    rodriguez_inv,
    tangent_map,
)
from cardillo.model.frame import Frame
from cardillo.model.bilateral_constraints.implicit import (
    RigidConnection,
    RigidConnectionCable,
)
from cardillo.beams import (
    animate_beam,
    TimoshenkoAxisAngle,
    TimoshenkoAxisAngleSE3,
)
from cardillo.forces import Force, K_Moment, DistributedForce1D
from cardillo.contacts import Line2Line
from cardillo.model import Model
from cardillo.solver import (
    Newton,
    ScipyIVP,
    GenAlphaFirstOrder,
    Moreau,
)

import numpy as np
import matplotlib.pyplot as plt


def quadratic_beam_material(E, G, cross_section, Beam):
    A = cross_section.area
    Ip, I2, I3 = np.diag(cross_section.second_moment)
    Ei = np.array([E * A, G * A, G * A])
    Fi = np.array([G * Ip, E * I2, E * I3])
    return Simo1986(Ei, Fi)


def beam_factory(
    nelements,
    polynomial_degree,
    nquadrature_points,
    shape_functions,
    cross_section,
    material_model,
    Beam,
    L,
    r_OP=np.zeros(3),
    A_IK=np.eye(3),
):
    ###############################
    # build reference configuration
    ###############################
    if Beam == TimoshenkoAxisAngle:
        p_r = polynomial_degree
        p_psi = p_r - 1
        Q = TimoshenkoAxisAngle.straight_configuration(
            p_r, p_psi, nelements, L, r_OP=r_OP, A_IK=A_IK, basis=shape_functions
        )
    elif Beam == TimoshenkoAxisAngleSE3:
        p_r = polynomial_degree
        p_psi = polynomial_degree
        Q = TimoshenkoAxisAngleSE3.straight_configuration(
            p_r, p_psi, nelements, L, r_OP=r_OP, A_IK=A_IK, basis=shape_functions
        )
    else:
        raise NotImplementedError("")

    # Initial configuration coincides with reference configuration.
    # Note: This might be adapted.
    q0 = Q.copy()

    # extract cross section properties
    # TODO: Maybe we should pass this to the beam model itself?
    area = cross_section.area
    line_density = cross_section.line_density
    first_moment = cross_section.first_moment
    second_moment = cross_section.second_moment

    # for constant line densities the required quantities are related to the
    # zeroth, first and second moment of area, see Harsch2021 footnote 2.
    # TODO: I think we made an error there since in the Wilberforce pendulum
    # example we used
    # C_rho0 = line_density * np.diag([0, I3, I2]).
    # I think it should be C_rho0^ab = rho0 * I_ba?
    # TODO: Compute again the relation between Binet inertia tensor and
    # standard one.
    A_rho0 = line_density * area
    B_rho0 = line_density * first_moment
    C_rho0 = line_density * second_moment
    # TODO: I think this is Binet's inertia tensor!
    # TODO: See Mäkinen2006, (24) on page 1022 for a clarification of the
    # classical inertia tensor
    C_rho0 = np.zeros((3, 3))
    for a in range(1, 3):
        for b in range(1, 3):
            C_rho0[a, b] = line_density * second_moment[b, a]

    # This is the standard second moment of area weighted by a constant line
    # density
    I_rho0 = line_density * second_moment

    ##################
    # build beam model
    ##################
    if Beam == TimoshenkoAxisAngle:
        beam = TimoshenkoAxisAngle(
            material_model,
            A_rho0,
            I_rho0,
            p_r,
            p_psi,
            nquadrature_points,
            nelements,
            Q=Q,
            q0=q0,
            basis=shape_functions,
        )
    elif Beam == TimoshenkoAxisAngleSE3:
        beam = TimoshenkoAxisAngleSE3(
            material_model,
            A_rho0,
            I_rho0,
            p_r,
            p_psi,
            nquadrature_points,
            nelements,
            Q=Q,
            q0=q0,
            basis=shape_functions,
        )
    else:
        raise NotImplementedError("")

    return beam


def run(statics):
    # Beam = TimoshenkoAxisAngle
    Beam = TimoshenkoAxisAngleSE3

    # number of elements
    nelements = 1
    # nelements = 2
    # nelements = 4
    # nelements = 8
    # nelements = 16
    # nelements = 32
    # nelements = 64

    # used polynomial degree
    polynomial_degree = 1
    # polynomial_degree = 2
    # polynomial_degree = 3
    # polynomial_degree = 5
    # polynomial_degree = 6

    # number of quadrature points
    # TODO: We have to distinguish between integration of the mass matrix,
    #       gyroscopic forces and potential forces!
    nquadrature_points = int(np.ceil((polynomial_degree + 1) ** 2 / 2))
    # nquadrature_points = polynomial_degree + 2
    # nquadrature_points = polynomial_degree + 1 # this seems not to be sufficent for p > 1

    # working combinations
    # - Bspline shape functions: "Absolute rotation vector with Crisfield's  relative interpolation":
    #   p = 2,3,5; p_r = p; p_psi = p - 1, nquadrature = p (Gauss-Legendre),
    #   nelements = 1,2,4; slenderness = 1.0e3
    # - cubic Hermite shape functions: "Absolute rotation vector with Crisfield's  relative interpolation":
    #   p_r = 3; p_psi = 1, nquadrature = 3 (Gauss-Lobatto),
    #   nelements = 1,2,4; slenderness = 1.0e3

    # used shape functions for discretization
    shape_functions = "B-spline"
    # shape_functions = "Lagrange"

    # used cross section
    # slenderness = 1
    # slenderness = 1.0e1
    # slenderness = 1.0e2
    slenderness = 1.0e3
    # slenderness = 1.0e4
    radius = 1
    # radius = 1.0e-0
    # radius = 1.0e-1
    # radius = 5.0e-2
    # radius = 1.0e-3 # this yields no deformation due to locking!
    line_density = 1
    cross_section = CircularCrossSection(line_density, radius)

    # Young's and shear modulus
    # E = 1.0e0
    E = 1.0e3
    nu = 0.5
    G = E / (2.0 * (1.0 + nu))

    # build quadratic material model
    material_model = quadratic_beam_material(E, G, cross_section, Beam)
    # print(f"Ei: {material_model.Ei}")
    print(f"Fi: {material_model.Fi}")

    # starting point and orientation of initial point, initial length
    r_OP = np.zeros(3)
    A_IK = np.eye(3)
    L = radius * slenderness

    # build beam model
    beam = beam_factory(
        nelements,
        polynomial_degree,
        nquadrature_points,
        shape_functions,
        cross_section,
        material_model,
        Beam,
        L,
        r_OP=r_OP,
        A_IK=A_IK,
    )

    # ############################################
    # # dummy values for debugging internal forces
    # ############################################
    # # assemble the model
    # model = Model()
    # model.add(beam)
    # model.assemble()

    # t = 0
    # q = model.q0
    # # q = np.random.rand(model.nq)

    # E_pot = model.E_pot(t, q)
    # print(f"E_pot: {E_pot}")
    # f_pot = model.f_pot(t, q)
    # print(f"f_pot:\n{f_pot}")
    # # f_pot_q = model.f_pot_q(t, q)
    # # print(f"f_pot_q:\n{f_pot_q}")

    # exit()

    # xis = np.linspace(0, 1, num=10)
    # for xi in xis:
    #     r_OC = beam.r_OC(t, q, frame_ID=(xi,))
    #     print(f"r_OC({xi}): {r_OC}")
    # for xi in xis:
    #     r_OC_xi = beam.r_OC_xi(t, q, frame_ID=(xi,))
    #     print(f"r_OC_xi({xi}): {r_OC_xi}")
    # exit()

    # number of full rotations after deformation
    # TODO: Allow zero circles!
    n_circles = 2
    frac_deformation = 1 / (n_circles + 1)
    frac_rotation = 1 - frac_deformation
    print(f"n_circles: {n_circles}")
    print(f"frac_deformation: {frac_deformation}")
    print(f"frac_rotation:     {frac_rotation}")

    # junctions
    r_OB0 = np.zeros(3)
    # r_OB0 = np.array([-1, 0.25, 3.14])
    if statics:
        # phi = (
        #     lambda t: n_circles * 2 * pi * smoothstep2(t, frac_deformation, 1.0)
        # )  # * 0.5
        # # phi2 = lambda t: pi / 4 * sin(2 * pi * smoothstep2(t, frac_deformation, 1.0))
        # # A_IK0 = lambda t: A_IK_basic(phi(t)).x()
        # # TODO: Get this strange rotation working with a full circle
        # A_IK0 = lambda t: A_IK_basic(phi(t)).z()
        # # A_IK0 = (
        # #     lambda t: A_IK_basic(0.5 * phi(t)).z()
        # #     @ A_IK_basic(0.5 * phi(t)).y()
        # #     @ A_IK_basic(phi(t)).x()
        # # )
        A_IK0 = lambda t: np.eye(3)
    else:
        # phi = lambda t: smoothstep2(t, 0, 0.1) * sin(0.3 * pi * t) * pi / 4
        phi = lambda t: smoothstep2(t, 0, 0.1) * sin(0.6 * pi * t) * pi / 4
        # A_IK0 = lambda t: A_IK_basic(phi(t)).z()
        A_IK0 = (
            lambda t: A_IK_basic(0.5 * phi(t)).z()
            @ A_IK_basic(0.5 * phi(t)).y()
            @ A_IK_basic(phi(t)).x()
        )
        # A_IK0 = lambda t: np.eye(3)

    frame1 = Frame(r_OP=r_OB0, A_IK=A_IK0)

    # left and right joint
    joint1 = RigidConnection(frame1, beam, r_OB0, frame_ID2=(0,))

    # gravity beam
    g = np.array([0, 0, -cross_section.area * cross_section.line_density * 9.81])
    f_g_beam = DistributedForce1D(lambda t, xi: g, beam)

    # moment at right end
    Fi = material_model.Fi
    # M = lambda t: np.array([1, 1, 0]) * smoothstep2(t, 0.0, frac_deformation) * 2 * np.pi * Fi[1] / L
    # M = lambda t: e1 * smoothstep2(t, 0.0, frac_deformation) * 2 * np.pi * Fi[0] / L * 1.0
    # M = lambda t: e2 * smoothstep2(t, 0.0, frac_deformation) * 2 * np.pi * Fi[1] / L * 0.75
    M = (
        lambda t: (e3 * Fi[2])
        # lambda t: (e1 * Fi[0] + e3 * Fi[2])
        * smoothstep2(t, 0.0, frac_deformation)
        * 2
        * np.pi
        / L
        # * 0.1
        * 0.25
        # * 0.5
        # * 0.75
    )
    moment = K_Moment(M, beam, (1,))

    # force at right end
    F = lambda t: np.array([0, 0, -1]) * t * 1.0e-2
    force = Force(F, beam, frame_ID=(1,))

    # assemble the model
    model = Model()
    model.add(beam)
    model.add(frame1)
    model.add(joint1)
    # model.add(force)
    if statics:
        model.add(moment)
    # else:
    #     model.add(f_g_beam)
    model.assemble()

    # t = np.array([0])
    # q = np.array([model.q0])
    # animate_beam(t, q, [beam], scale=L)
    # exit()

    if statics:
        solver = Newton(
            model,
            # n_load_steps=10,
            # n_load_steps=50,
            n_load_steps=100,
            # n_load_steps=500,
            max_iter=30,
            # atol=1.0e-4,
            atol=1.0e-6,
            # atol=1.0e-8,
            # atol=1.0e-10,
            numerical_jacobian=False,
        )
    else:
        t1 = 1.0
        # t1 = 10.0
        dt = 5.0e-2
        # dt = 2.5e-2
        method = "RK45"
        rtol = 1.0e-6
        atol = 1.0e-6
        rho_inf = 0.5

        # solver = ScipyIVP(model, t1, dt, method=method, rtol=rtol, atol=atol) # this is no good idea for Runge-Kutta solvers
        solver = GenAlphaFirstOrder(model, t1, dt, rho_inf=rho_inf, tol=atol)
        # solver = GenAlphaDAEAcc(model, t1, dt, rho_inf=rho_inf, newton_tol=atol)
        # dt = 5.0e-3
        # solver = Moreau(model, t1, dt)

    sol = solver.solve()
    q = sol.q
    nt = len(q)
    t = sol.t[:nt]

    # if nelements == 1:
    # visualize nodal rotation vectors
    fig, ax = plt.subplots()

    for i, nodalDOF_psi in enumerate(beam.nodalDOF_psi):
        psi = q[:, beam.qDOF[nodalDOF_psi]]
        ax.plot(t, np.linalg.norm(psi, axis=1), label=f"||psi{i}||")

    ax.set_xlabel("t")
    ax.set_ylabel("nodal rotation vectors")
    ax.grid()
    ax.legend()

    ################################
    # visualize norm strain measures
    ################################
    fig, ax = plt.subplots(1, 2)

    nxi = 1000
    xis = np.linspace(0, 1, num=nxi)

    K_Gamma = np.zeros((3, nxi))
    K_Kappa = np.zeros((3, nxi))
    for i in range(nxi):
        frame_ID = (xis[i],)
        elDOF = beam.qDOF_P(frame_ID)
        qe = q[-1, beam.qDOF][elDOF]
        _, _, K_Gamma[:, i], K_Kappa[:, i] = beam.eval(qe, xis[i])
    ax[0].plot(xis, K_Gamma[0], "-r", label="K_Gamma0")
    ax[0].plot(xis, K_Gamma[1], "-g", label="K_Gamma1")
    ax[0].plot(xis, K_Gamma[2], "-b", label="K_Gamma2")
    ax[0].grid()
    ax[0].legend()
    ax[1].plot(xis, K_Kappa[0], "-r", label="K_Kappa0")
    ax[1].plot(xis, K_Kappa[1], "-g", label="K_Kappa1")
    ax[1].plot(xis, K_Kappa[2], "-b", label="K_Kappa2")
    ax[1].grid()
    ax[1].legend()

    # ########################################################
    # # visualize norm of tangent vector and quadrature points
    # ########################################################
    # fig, ax = plt.subplots()

    # nxi = 1000
    # xis = np.linspace(0, 1, num=nxi)

    # abs_r_xi = np.zeros(nxi)
    # abs_r0_xi = np.zeros(nxi)
    # for i in range(nxi):
    #     frame_ID = (xis[i],)
    #     elDOF = beam.qDOF_P(frame_ID)
    #     qe = q[-1, beam.qDOF][elDOF]
    #     abs_r_xi[i] = np.linalg.norm(beam.r_OC_xi(t[-1], qe, frame_ID))
    #     q0e = q[0, beam.qDOF][elDOF]
    #     abs_r0_xi[i] = np.linalg.norm(beam.r_OC_xi(t[0], q0e, frame_ID))
    # ax.plot(xis, abs_r_xi, "-r", label="||r_xi||")
    # ax.plot(xis, abs_r0_xi, "--b", label="||r0_xi||")
    # ax.set_xlabel("xi")
    # ax.set_ylabel("||r_xi||")
    # ax.grid()
    # ax.legend()

    # # compute quadrature points
    # for el in range(beam.nelement):
    #     elDOF = beam.elDOF[el]
    #     q0e = q[0, beam.qDOF][elDOF]
    #     for i in range(beam.nquadrature):
    #         xi = beam.qp[el, i]
    #         abs_r0_xi = np.linalg.norm(beam.r_OC_xi(t[0], q0e, (xi,)))
    #         ax.plot(xi, abs_r0_xi, "xr")

    # plt.show()
    # exit()

    ############################
    # Visualize potential energy
    ############################
    E_pot = np.array([model.E_pot(ti, qi) for (ti, qi) in zip(t, q)])

    fig, ax = plt.subplots(1, 2)

    ax[0].plot(t, E_pot)
    ax[0].set_xlabel("t")
    ax[0].set_ylabel("E_pot")
    ax[0].grid()

    idx = np.where(t > frac_deformation)[0]
    ax[1].plot(t[idx], E_pot[idx])
    ax[1].set_xlabel("t")
    ax[1].set_ylabel("E_pot")
    ax[1].grid()

    # visualize final centerline projected in all three planes
    r_OPs = beam.centerline(q[-1])
    fig, ax = plt.subplots(1, 3)
    ax[0].plot(r_OPs[0, :], r_OPs[1, :], label="x-y")
    ax[1].plot(r_OPs[1, :], r_OPs[2, :], label="y-z")
    ax[2].plot(r_OPs[2, :], r_OPs[0, :], label="z-x")
    ax[0].grid()
    ax[0].legend()
    ax[0].set_aspect(1)
    ax[1].grid()
    ax[1].legend()
    ax[1].set_aspect(1)
    ax[2].grid()
    ax[2].legend()
    ax[2].set_aspect(1)

    ###########
    # animation
    ###########
    animate_beam(t, q, [beam], L, show=True)


def locking():
    # Beam = TimoshenkoAxisAngle
    Beam = TimoshenkoAxisAngleSE3

    # number of elements
    nelements = 1
    # nelements = 2
    # nelements = 4
    # nelements = 8
    # nelements = 16
    # nelements = 32
    # nelements = 64

    # used polynomial degree
    polynomial_degree = 1
    # polynomial_degree = 2
    # polynomial_degree = 3
    # polynomial_degree = 5
    # polynomial_degree = 6

    # number of quadrature points
    # TODO: We have to distinguish between integration of the mass matrix,
    #       gyroscopic forces and potential forces!
    nquadrature_points = int(np.ceil((polynomial_degree + 1) ** 2 / 2))
    # nquadrature_points = polynomial_degree + 2
    # nquadrature_points = polynomial_degree + 1 # this seems not to be sufficent for p > 1

    # used shape functions for discretization
    shape_functions = "B-spline"
    # shape_functions = "Lagrange"

    # beam length
    # L = 1.0e3 # Meier2015
    L = 1

    # used cross section
    slenderness = 1
    # slenderness = 1.0e1
    # slenderness = 1.0e2
    # slenderness = 1.0e3
    # slenderness = 1.0e4
    radius = 1
    # radius = 1.0e-0
    # radius = 1.0e-1
    # radius = 5.0e-2
    # radius = 1.0e-3 # this yields no deformation due to locking!
    line_density = 1

    radius = L / slenderness  # Meier2015

    cross_section = CircularCrossSection(line_density, radius)

    # Young's and shear modulus
    E = 1.0  # Meier2015
    G = 0.5  # Meier2015
    # # E = 1.0e1
    # nu = 0.5
    # G = E / (2.0 * (1.0 + nu))

    # build quadratic material model
    material_model = quadratic_beam_material(E, G, cross_section, Beam)
    print(f"Ei: {material_model.Ei}")
    print(f"Fi: {material_model.Fi}")

    # starting point and orientation of initial point, initial length
    r_OP = np.zeros(3)
    A_IK = np.eye(3)
    L = radius * slenderness

    # build beam model
    beam = beam_factory(
        nelements,
        polynomial_degree,
        nquadrature_points,
        shape_functions,
        cross_section,
        material_model,
        Beam,
        L,
        r_OP=r_OP,
        A_IK=A_IK,
    )

    # junctions
    r_OB0 = np.zeros(3)
    A_IK0 = lambda t: np.eye(3)
    frame1 = Frame(r_OP=r_OB0, A_IK=A_IK0)

    # left and right joint
    joint1 = RigidConnection(frame1, beam, r_OB0, frame_ID2=(0,))

    # moment at right end
    Fi = material_model.Fi
    M = (
        lambda t: (e1 * Fi[0])
        # lambda t: (e2 * Fi[1])
        # lambda t: (e3 * Fi[2])
        # lambda t: (e1 * Fi[0] + e3 * Fi[2]) * 1.0e-1
        * smoothstep2(t, 0.0, 1)
        * 2
        * np.pi
        / L
        * 0.25
    )
    moment = K_Moment(M, beam, (1,))

    # assemble the model
    model = Model()
    model.add(beam)
    model.add(frame1)
    model.add(joint1)
    model.add(moment)
    model.assemble()

    solver = Newton(
        model,
        # n_load_steps=10,
        # n_load_steps=50,
        n_load_steps=100,
        # n_load_steps=500,
        max_iter=30,
        # atol=1.0e-4,
        atol=1.0e-6,
        # atol=1.0e-8,
        # atol=1.0e-10,
        numerical_jacobian=False,
    )
    sol = solver.solve()
    q = sol.q
    nt = len(q)
    t = sol.t[:nt]

    # visualize nodal rotation vectors
    fig, ax = plt.subplots()

    for i, nodalDOF_psi in enumerate(beam.nodalDOF_psi):
        psi = q[:, beam.qDOF[nodalDOF_psi]]
        ax.plot(t, np.linalg.norm(psi, axis=1), label=f"||psi{i}||")

    ax.set_xlabel("t")
    ax.set_ylabel("nodal rotation vectors")
    ax.grid()
    ax.legend()

    ################################
    # visualize norm strain measures
    ################################
    fig, ax = plt.subplots(1, 2)

    nxi = 1000
    xis = np.linspace(0, 1, num=nxi)

    K_Gamma = np.zeros((3, nxi))
    K_Kappa = np.zeros((3, nxi))
    for i in range(nxi):
        frame_ID = (xis[i],)
        elDOF = beam.qDOF_P(frame_ID)
        qe = q[-1, beam.qDOF][elDOF]
        _, _, K_Gamma[:, i], K_Kappa[:, i] = beam.eval(qe, xis[i])
    ax[0].plot(xis, K_Gamma[0], "-r", label="K_Gamma0")
    ax[0].plot(xis, K_Gamma[1], "-g", label="K_Gamma1")
    ax[0].plot(xis, K_Gamma[2], "-b", label="K_Gamma2")
    ax[0].grid()
    ax[0].legend()
    ax[1].plot(xis, K_Kappa[0], "-r", label="K_Kappa0")
    ax[1].plot(xis, K_Kappa[1], "-g", label="K_Kappa1")
    ax[1].plot(xis, K_Kappa[2], "-b", label="K_Kappa2")
    ax[1].grid()
    ax[1].legend()

    # ########################################################
    # # visualize norm of tangent vector and quadrature points
    # ########################################################
    # fig, ax = plt.subplots()

    # nxi = 1000
    # xis = np.linspace(0, 1, num=nxi)

    # abs_r_xi = np.zeros(nxi)
    # abs_r0_xi = np.zeros(nxi)
    # for i in range(nxi):
    #     frame_ID = (xis[i],)
    #     elDOF = beam.qDOF_P(frame_ID)
    #     qe = q[-1, beam.qDOF][elDOF]
    #     abs_r_xi[i] = np.linalg.norm(beam.r_OC_xi(t[-1], qe, frame_ID))
    #     q0e = q[0, beam.qDOF][elDOF]
    #     abs_r0_xi[i] = np.linalg.norm(beam.r_OC_xi(t[0], q0e, frame_ID))
    # ax.plot(xis, abs_r_xi, "-r", label="||r_xi||")
    # ax.plot(xis, abs_r0_xi, "--b", label="||r0_xi||")
    # ax.set_xlabel("xi")
    # ax.set_ylabel("||r_xi||")
    # ax.grid()
    # ax.legend()

    # # compute quadrature points
    # for el in range(beam.nelement):
    #     elDOF = beam.elDOF[el]
    #     q0e = q[0, beam.qDOF][elDOF]
    #     for i in range(beam.nquadrature):
    #         xi = beam.qp[el, i]
    #         abs_r0_xi = np.linalg.norm(beam.r_OC_xi(t[0], q0e, (xi,)))
    #         ax.plot(xi, abs_r0_xi, "xr")

    # plt.show()
    # exit()

    ############################
    # Visualize potential energy
    ############################
    E_pot = np.array([model.E_pot(ti, qi) for (ti, qi) in zip(t, q)])

    fig, ax = plt.subplots(1, 2)

    ax[0].plot(t, E_pot)
    ax[0].set_xlabel("t")
    ax[0].set_ylabel("E_pot")
    ax[0].grid()

    # visualize final centerline projected in all three planes
    r_OPs = beam.centerline(q[-1])
    fig, ax = plt.subplots(1, 3)
    ax[0].plot(r_OPs[0, :], r_OPs[1, :], label="x-y")
    ax[1].plot(r_OPs[1, :], r_OPs[2, :], label="y-z")
    ax[2].plot(r_OPs[2, :], r_OPs[0, :], label="z-x")
    ax[0].grid()
    ax[0].legend()
    ax[0].set_aspect(1)
    ax[1].grid()
    ax[1].legend()
    ax[1].set_aspect(1)
    ax[2].grid()
    ax[2].legend()
    ax[2].set_aspect(1)

    ###########
    # animation
    ###########
    animate_beam(t, q, [beam], L, show=True)


def SE3_interpolation():
    def SE3(R, r):
        H = np.zeros((4, 4), dtype=float)
        H[:3, :3] = R
        H[:3, 3] = r
        H[3, 3] = 1.0
        return H

    def SE3inv(H):
        R = H[:3, :3]
        r = H[:3, 3]
        return SE3(R.T, -R.T @ r)  # Sonneville2013 (12)

    def SE3log(H):
        """See Murray1994 Example A.14."""
        R = H[:3, :3]
        r = H[:3, 3]

        # log SO(3)
        psi = rodriguez_inv(R)

        # inverse tangent map
        psi2 = psi @ psi
        A_inv = np.eye(3, dtype=float)
        if psi2 > 0:
            abs_psi = sqrt(psi2)
            psi_tilde = ax2skew(psi)
            A_inv += (
                -0.5 * psi_tilde
                + (2.0 * sin(abs_psi) - abs_psi * (1.0 + cos(abs_psi)))
                / (2.0 * psi2 * sin(abs_psi))
                * psi_tilde
                @ psi_tilde
            )
            # A_inv = inverse_tangent_map(psi).T # Sonneville2013 (A.15)

        h = np.concatenate((A_inv @ r, psi))
        return h

    def se3exp(h):
        """See Murray1994 Example A.12."""
        r = h[:3]
        psi = h[3:]
        psi2 = psi @ psi

        H = np.eye(4, dtype=float)
        if psi2 > 0:
            # exp SO(3)
            H[:3, :3] = rodriguez(psi)

            # tangent map
            abs_psi = sqrt(psi2)
            psi_tilde = ax2skew(psi)
            A = (
                np.eye(3, dtype=float)
                + (1.0 - cos(abs_psi)) / psi2 * psi_tilde
                + (abs_psi - sin(abs_psi)) / (abs_psi * psi2) * psi_tilde @ psi_tilde
            )
            # A = tangent_map(psi).T # Sonneville2013 (A.10)

            H[:3, 3] = A @ r
        else:
            H[:3, 3] = r

        return H

    def interp1(r_OA, r_OB, psi_A, psi_B, xi):
        # evaluate rodriguez formular for both nodes
        A_IA = rodriguez(psi_A)
        A_IB = rodriguez(psi_B)

        # nodal SE(3) objects
        H_IA = SE3(A_IA, r_OA)
        H_IB = SE3(A_IB, r_OB)

        # invert H_IA
        H_AI = SE3(A_IA.T, -A_IA.T @ r_OA)
        H_BI = SE3(A_IB.T, -A_IB.T @ r_OB)
        assert np.allclose(H_IA @ H_AI, np.eye(4, dtype=float))
        assert np.allclose(H_IB @ H_BI, np.eye(4, dtype=float))

        ########################
        # reference SE(3) object
        ########################
        # (a): left node
        H_IR = H_IA

        # (b): right node
        H_IR = H_IB

        # (c): midway node
        H_IR = H_IA @ se3exp(0.5 * SE3log(SE3inv(H_IA) @ H_IB))

        # evaluate inverse reference SE(3) object
        H_RI = SE3inv(H_IR)

        H_nodes = [H_IA, H_IB]
        h_rel = np.zeros(6)
        for node in range(2):
            # current SE(3) object
            H_IK = H_nodes[node]

            # relative SE(3)/ se(3) objects
            H_RK = H_RI @ H_IK
            h_RK = SE3log(H_RK)

            # relative interpolation of se(3) using linear shape functions
            if node == 0:
                h_rel += (1.0 - xi) * h_RK
            else:
                h_rel += xi * h_RK

        # composition of reference rotation and relative one
        H_IK = H_IR @ se3exp(h_rel)

        # extract se(3) object
        h_IK = SE3log(H_IK)
        return h_IK, H_IK

    r_OA = np.zeros(3, dtype=float)
    r_OB = np.sqrt(2.0) / 2.0 * np.array([1, 1, 0], dtype=float)

    psi_A = np.zeros(3, dtype=float)
    psi_B = np.pi / 4.0 * np.array([0, 0, 1], dtype=float)

    num = 10
    xis = np.linspace(0, 1, num=num)

    h = np.zeros((num, 6))
    H = np.zeros((num, 4, 4))
    for i, xi in enumerate(xis):
        h[i], H[i] = interp1(r_OA, r_OB, psi_A, psi_B, xi)

    # centerline
    r_OP = h[:, :3].T

    # rotation vector
    psi = h[:, 3:].T

    # directors
    A_IK = H[:, :3, :3].T
    # A_IK = np.array([
    #     rodriguez(psii) for psii in psi
    # ])
    d1, d2, d3 = A_IK

    fig, ax = plt.subplots()
    ax.plot(r_OP[0], r_OP[1], "-k")
    ax.quiver(*r_OP[:2], *d1[:2], color="red")
    ax.quiver(*r_OP[:2], *d2[:2], color="green")
    ax.axis("equal")
    ax.grid(True)
    plt.show()


if __name__ == "__main__":
    # run(statics=True)
    locking()
    # SE3_interpolation()
