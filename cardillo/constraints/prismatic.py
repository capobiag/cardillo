import numpy as np
from cardillo.constraints._base import ProjectedPositionOrientationBase


class Prismatic(ProjectedPositionOrientationBase):
    def __init__(
        self,
        subsystem1,
        subsystem2,
        free_axis,
        r_OB0=None,
        A_IB0=None,
        frame_ID1=np.zeros(3),
        frame_ID2=np.zeros(3),
    ):
        assert free_axis in (0, 1, 2)

        # remove free axis
        constrained_axes_displacement = np.delete((0, 1, 2), free_axis)

        # all orientations are constrained
        projection_pairs_rotation = [(0, 1), (1, 2), (2, 0)]

        super().__init__(
            subsystem1,
            subsystem2,
            r_OB0=r_OB0,
            A_IB0=A_IB0,
            constrained_axes_displacement=constrained_axes_displacement,
            projection_pairs_rotation=projection_pairs_rotation,
            frame_ID1=frame_ID1,
            frame_ID2=frame_ID2,
        )
