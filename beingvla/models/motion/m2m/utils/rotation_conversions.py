import cv2
import numpy as np
import torch
import math
from torch.nn import functional as F

"""
Taken from https://pytorch3d.readthedocs.io/en/latest/_modules/pytorch3d/transforms/rotation_conversions.html
Just to avoid installing pytorch3d at times
"""

@torch.jit.script
def world_to_camera(rot, trans, camera):
    """
    Convert rotation and translation from world coordinates to camera coordinates.
    Args:
        rot (torch.Tensor): Rotation in world coordinates, shape (N, 3, 3).
        trans (torch.Tensor): Translation in world coordinates, shape (N, 3).
        camera (torch.Tensor): Camera extrinsics, shape (4, 4).

    Returns:
        Tuple[torch.Tensor, torch.Tensor]: Rotated and translated tensors in camera coordinates.
    """
    
    if len(camera.shape) == 2:
        camera = camera.unsqueeze(0)  # Ensure camera is (1, 4, 4)
    if camera.shape[0] == 1:
        Rc_w = camera[:, :3, :3].expand(rot.shape[0], -1, -1)  # (N, 3, 3)
        tc_w = camera[:, :3, 3].repeat(rot.shape[0], 1)  # (N, 3)
    else:
        Rc_w = camera[:, :3, :3]
        tc_w = camera[:, :3, 3]
        
    Rw_c = Rc_w.transpose(-1, -2)

    rot_c = torch.bmm(Rw_c, rot)
    trans_c = torch.bmm(Rw_c, (trans - tc_w).unsqueeze(-1)).squeeze(-1)

    return rot_c, trans_c



def standardize_quaternion(quaternions: torch.Tensor) -> torch.Tensor:
    """
    Convert a unit quaternion to a standard form: one in which the real
    part is non negative.

    Args:
        quaternions: Quaternions with real part first,
            as tensor of shape (..., 4).

    Returns:
        Standardized quaternions as tensor of shape (..., 4).
    """
    return torch.where(quaternions[..., 0:1] < 0, -quaternions, quaternions)


def quaternion_multiply(a: torch.Tensor, b: torch.Tensor) -> torch.Tensor:
    """
    Multiply two quaternions representing rotations, returning the quaternion
    representing their composition, i.e. the versor with nonnegative real part.
    Usual torch rules for broadcasting apply.

    Args:
        a: Quaternions as tensor of shape (..., 4), real part first.
        b: Quaternions as tensor of shape (..., 4), real part first.

    Returns:
        The product of a and b, a tensor of quaternions of shape (..., 4).
    """
    ab = quaternion_raw_multiply(a, b)
    return standardize_quaternion(ab)


def _sqrt_positive_part(x: torch.Tensor) -> torch.Tensor:
    """
    Returns torch.sqrt(torch.max(0, x))
    but with a zero subgradient where x is 0.
    """
    ret = torch.zeros_like(x)
    positive_mask = x > 0
    ret[positive_mask] = torch.sqrt(x[positive_mask])
    return ret

@torch.jit.script
def quaternion_to_axis_angle(quaternions: torch.Tensor) -> torch.Tensor:
    """
    Convert rotations given as quaternions to axis/angle.

    Args:
        quaternions: quaternions with real part first,
            as tensor of shape (..., 4) [w,x,y,z].

    Returns:
        Rotations given as a vector in axis angle form, as a tensor
            of shape (..., 3), where the magnitude is the angle
            turned anticlockwise in radians around the vector's
            direction.
    """
    norms = torch.norm(quaternions[..., 1:], p=2, dim=-1, keepdim=True)
    half_angles = torch.atan2(norms, quaternions[..., :1])
    angles = 2 * half_angles
    eps = 1e-6
    small_angles = angles.abs() < eps
    sin_half_angles_over_angles = torch.empty_like(angles)
    sin_half_angles_over_angles[~small_angles] = (
        torch.sin(half_angles[~small_angles]) / angles[~small_angles]
    )
    # for x small, sin(x/2) is about x/2 - (x/2)^3/6
    # so sin(x/2)/x is about 1/2 - (x*x)/48
    sin_half_angles_over_angles[small_angles] = (
        0.5 - (angles[small_angles] * angles[small_angles]) / 48
    )
    return quaternions[..., 1:] / sin_half_angles_over_angles


def quaternion_to_matrix(quaternions: torch.Tensor) -> torch.Tensor:
    """
    Convert rotations given as quaternions to rotation matrices.

    Args:
        quaternions: quaternions with real part first,
            as tensor of shape (..., 4).

    Returns:
        Rotation matrices as tensor of shape (..., 3, 3).
    """
    r, i, j, k = torch.unbind(quaternions, -1)
    # pyre-fixme[58]: `/` is not supported for operand types `float` and `Tensor`.
    two_s = 2.0 / (quaternions * quaternions).sum(-1)

    o = torch.stack(
        (
            1 - two_s * (j * j + k * k),
            two_s * (i * j - k * r),
            two_s * (i * k + j * r),
            two_s * (i * j + k * r),
            1 - two_s * (i * i + k * k),
            two_s * (j * k - i * r),
            two_s * (i * k - j * r),
            two_s * (j * k + i * r),
            1 - two_s * (i * i + j * j),
        ),
        -1,
    )
    return o.reshape(quaternions.shape[:-1] + (3, 3))

@torch.jit.script
def matrix_to_quaternion(matrix: torch.Tensor) -> torch.Tensor:
    """
    Convert rotations given as rotation matrices to quaternions.

    Args:
        matrix: Rotation matrices as tensor of shape (..., 3, 3).

    Returns:
        quaternions with real part first, as tensor of shape (..., 4).
    """
    if matrix.size(-1) != 3 or matrix.size(-2) != 3:
        raise ValueError(f"Invalid rotation matrix shape {matrix.shape}.")

    batch_dim = matrix.shape[:-2]
    m00, m01, m02, m10, m11, m12, m20, m21, m22 = torch.unbind(
        matrix.reshape(batch_dim + (9,)), dim=-1
    )

    q_abs = _sqrt_positive_part(
        torch.stack(
            [
                1.0 + m00 + m11 + m22,
                1.0 + m00 - m11 - m22,
                1.0 - m00 + m11 - m22,
                1.0 - m00 - m11 + m22,
            ],
            dim=-1,
        )
    )

    # we produce the desired quaternion multiplied by each of r, i, j, k
    quat_by_rijk = torch.stack(
        [
            # pyre-fixme[58]: `**` is not supported for operand types `Tensor` and
            #  `int`.
            torch.stack([q_abs[..., 0] ** 2, m21 - m12, m02 - m20, m10 - m01], dim=-1),
            # pyre-fixme[58]: `**` is not supported for operand types `Tensor` and
            #  `int`.
            torch.stack([m21 - m12, q_abs[..., 1] ** 2, m10 + m01, m02 + m20], dim=-1),
            # pyre-fixme[58]: `**` is not supported for operand types `Tensor` and
            #  `int`.
            torch.stack([m02 - m20, m10 + m01, q_abs[..., 2] ** 2, m12 + m21], dim=-1),
            # pyre-fixme[58]: `**` is not supported for operand types `Tensor` and
            #  `int`.
            torch.stack([m10 - m01, m20 + m02, m21 + m12, q_abs[..., 3] ** 2], dim=-1),
        ],
        dim=-2,
    )

    # We floor here at 0.1 but the exact level is not important; if q_abs is small,
    # the candidate won't be picked.
    flr = torch.tensor(0.1).to(dtype=q_abs.dtype, device=q_abs.device)
    quat_candidates = quat_by_rijk / (2.0 * q_abs[..., None].max(flr))

    # if not for numerical problems, quat_candidates[i] should be same (up to a sign),
    # forall i; we pick the best-conditioned one (with the largest denominator)

    return quat_candidates[
        F.one_hot(q_abs.argmax(dim=-1), num_classes=4) > 0.5, :
    ].reshape(batch_dim + (4,))

@torch.jit.script
def matrix_to_axis_angle(matrix: torch.Tensor) -> torch.Tensor:
    """
    Convert rotations given as rotation matrices to axis/angle.

    Args:
        matrix: Rotation matrices as tensor of shape (..., 3, 3).

    Returns:
        Rotations given as a vector in axis angle form, as a tensor
            of shape (..., 3), where the magnitude is the angle
            turned anticlockwise in radians around the vector's
            direction.
    """
    return quaternion_to_axis_angle(matrix_to_quaternion(matrix))


def rot_aa(aa, rot):
    """Rotate axis angle parameters."""
    # pose parameters
    R = np.array(
        [
            [np.cos(np.deg2rad(-rot)), -np.sin(np.deg2rad(-rot)), 0],
            [np.sin(np.deg2rad(-rot)), np.cos(np.deg2rad(-rot)), 0],
            [0, 0, 1],
        ]
    )
    # find the rotation of the body in camera frame
    per_rdg, _ = cv2.Rodrigues(aa)
    # apply the global rotation to the global orientation
    resrot, _ = cv2.Rodrigues(np.dot(R, per_rdg))
    aa = (resrot.T)[0]
    return aa

@torch.jit.script
def quat2mat(quat):
    """
    This function is borrowed from https://github.com/MandyMo/pytorch_HMR/blob/master/src/util.py#L50
    Convert quaternion coefficients to rotation matrix.
    Args:
        quat: size = [batch_size, 4] 4 <===>(w, x, y, z)
    Returns:
        Rotation matrix corresponding to the quaternion -- size = [batch_size, 3, 3]
    """
    norm_quat = quat
    norm_quat = norm_quat / norm_quat.norm(p=2, dim=1, keepdim=True)
    w, x, y, z = norm_quat[:, 0], norm_quat[:, 1], norm_quat[:, 2], norm_quat[:, 3]

    batch_size = quat.size(0)

    w2, x2, y2, z2 = w.pow(2), x.pow(2), y.pow(2), z.pow(2)
    wx, wy, wz = w * x, w * y, w * z
    xy, xz, yz = x * y, x * z, y * z

    rotMat = torch.stack(
        [
            w2 + x2 - y2 - z2,
            2 * xy - 2 * wz,
            2 * wy + 2 * xz,
            2 * wz + 2 * xy,
            w2 - x2 + y2 - z2,
            2 * yz - 2 * wx,
            2 * xz - 2 * wy,
            2 * wx + 2 * yz,
            w2 - x2 - y2 + z2,
        ],
        dim=1,
    ).view(batch_size, 3, 3)
    return rotMat


@torch.jit.script
def batch_aa2rotmat(axisang):
    # This function is borrowed from https://github.com/MandyMo/pytorch_HMR/blob/master/src/util.py#L37
    assert len(axisang.shape) == 2
    assert axisang.shape[1] == 3
    # axisang N x 3
    axisang_norm = torch.norm(axisang + 1e-8, p=2, dim=1)
    angle = torch.unsqueeze(axisang_norm, -1)
    axisang_normalized = torch.div(axisang, angle)
    angle = angle * 0.5
    v_cos = torch.cos(angle)
    v_sin = torch.sin(angle)
    quat = torch.cat([v_cos, v_sin * axisang_normalized], dim=1)
    rot_mat = quat2mat(quat)
    # rot_mat = rot_mat.view(rot_mat.shape[0], 9)
    return rot_mat

@torch.jit.script
def batch_rot2aa(Rs):
    assert len(Rs.shape) == 3
    assert Rs.shape[1] == Rs.shape[2]
    assert Rs.shape[1] == 3

    """
    Rs is B x 3 x 3
    void cMathUtil::RotMatToAxisAngle(const tMatrix& mat, tVector& out_axis,
                                      double& out_theta)
    {
        double c = 0.5 * (mat(0, 0) + mat(1, 1) + mat(2, 2) - 1);
        c = cMathUtil::Clamp(c, -1.0, 1.0);

        out_theta = std::acos(c);

        if (std::abs(out_theta) < 0.00001)
        {
            out_axis = tVector(0, 0, 1, 0);
        }
        else
        {
            double m21 = mat(2, 1) - mat(1, 2);
            double m02 = mat(0, 2) - mat(2, 0);
            double m10 = mat(1, 0) - mat(0, 1);
            double denom = std::sqrt(m21 * m21 + m02 * m02 + m10 * m10);
            out_axis[0] = m21 / denom;
            out_axis[1] = m02 / denom;
            out_axis[2] = m10 / denom;
            out_axis[3] = 0;
        }
    }
    """
    cos = 0.5 * (torch.stack([torch.trace(x) for x in Rs]) - 1)
    cos = torch.clamp(cos, -1, 1)

    theta = torch.acos(cos)

    m21 = Rs[:, 2, 1] - Rs[:, 1, 2]
    m02 = Rs[:, 0, 2] - Rs[:, 2, 0]
    m10 = Rs[:, 1, 0] - Rs[:, 0, 1]
    denom = torch.sqrt(m21 * m21 + m02 * m02 + m10 * m10)

    axis0 = torch.where(torch.abs(theta) < 0.00001, m21, m21 / denom)
    axis1 = torch.where(torch.abs(theta) < 0.00001, m02, m02 / denom)
    axis2 = torch.where(torch.abs(theta) < 0.00001, m10, m10 / denom)

    return theta.unsqueeze(1) * torch.stack([axis0, axis1, axis2], 1)

@torch.jit.script
def quat_to_rotmat(quat):
    """Convert quaternion coefficients to rotation matrix.
    Args:
        quat: size = [B, 4] 4 <===>(w, x, y, z)
    Returns:
        Rotation matrix corresponding to the quaternion -- size = [B, 3, 3]
    """
    norm_quat = quat
    norm_quat = norm_quat / norm_quat.norm(p=2, dim=1, keepdim=True)
    w, x, y, z = norm_quat[:, 0], norm_quat[:, 1], norm_quat[:, 2], norm_quat[:, 3]

    B = quat.size(0)

    w2, x2, y2, z2 = w.pow(2), x.pow(2), y.pow(2), z.pow(2)
    wx, wy, wz = w * x, w * y, w * z
    xy, xz, yz = x * y, x * z, y * z

    rotMat = torch.stack(
        [
            w2 + x2 - y2 - z2,
            2 * xy - 2 * wz,
            2 * wy + 2 * xz,
            2 * wz + 2 * xy,
            w2 - x2 + y2 - z2,
            2 * yz - 2 * wx,
            2 * xz - 2 * wy,
            2 * wx + 2 * yz,
            w2 - x2 - y2 + z2,
        ],
        dim=1,
    ).view(B, 3, 3)
    return rotMat

@torch.jit.script
def batch_rodrigues(theta):
    """Convert axis-angle representation to rotation matrix.
    Args:
        theta: size = [B, 3]
    Returns:
        Rotation matrix corresponding to the quaternion -- size = [B, 3, 3]
    """
    l1norm = torch.norm(theta + 1e-8, p=2, dim=1)
    angle = torch.unsqueeze(l1norm, -1)
    normalized = torch.div(theta, angle)
    angle = angle * 0.5
    v_cos = torch.cos(angle)
    v_sin = torch.sin(angle)
    quat = torch.cat([v_cos, v_sin * normalized], dim=1)
    return quat_to_rotmat(quat)


@torch.jit.script
def rot6d_to_rotmat(x):
    """Convert 6D rotation representation to 3x3 rotation matrix.
    Based on Zhou et al., "On the Continuity of Rotation Representations in Neural Networks", CVPR 2019
    Input:
        (B,6) Batch of 6-D rotation representations
    Output:
        (B,3,3) Batch of corresponding rotation matrices
    """
    x = x.reshape(-1, 3, 2)
    a1 = x[:, :, 0]
    a2 = x[:, :, 1]
    b1 = F.normalize(a1)
    b2 = F.normalize(a2 - torch.einsum("bi,bi->b", b1, a2).unsqueeze(-1) * b1)
    b3 = torch.cross(b1, b2)
    return torch.stack((b1, b2, b3), dim=-1)

@torch.jit.script
def rotmat_to_rot6d(x):
    rotmat = x.reshape(-1, 3, 3)
    rot6d = rotmat[:, :, :2].reshape(x.shape[0], -1)
    return rot6d

@torch.jit.script
def quaternion_to_angle_axis(quaternion: torch.Tensor) -> torch.Tensor:
    """
    This function is borrowed from https://github.com/kornia/kornia

    Convert quaternion vector to angle axis of rotation.

    Adapted from ceres C++ library: ceres-solver/include/ceres/rotation.h

    Args:
        quaternion (torch.Tensor): tensor with quaternions.

    Return:
        torch.Tensor: tensor with angle axis of rotation.

    Shape:
        - Input: :math:`(*, 4)` where `*` means, any number of dimensions
        - Output: :math:`(*, 3)`

    Example:
        >>> quaternion = torch.rand(2, 4)  # Nx4
        >>> angle_axis = tgm.quaternion_to_angle_axis(quaternion)  # Nx3
    """
    # if not torch.is_tensor(quaternion):
    #     raise TypeError(
    #         "Input type is not a torch.Tensor. Got {}".format(type(quaternion))
    #     )

    if not quaternion.shape[-1] == 4:
        raise ValueError(
            "Input must be a tensor of shape Nx4 or 4. Got {}".format(quaternion.shape)
        )
    # unpack input and compute conversion
    q1: torch.Tensor = quaternion[..., 1]
    q2: torch.Tensor = quaternion[..., 2]
    q3: torch.Tensor = quaternion[..., 3]
    sin_squared_theta: torch.Tensor = q1 * q1 + q2 * q2 + q3 * q3

    sin_theta: torch.Tensor = torch.sqrt(sin_squared_theta)
    cos_theta: torch.Tensor = quaternion[..., 0]
    two_theta: torch.Tensor = 2.0 * torch.where(
        cos_theta < 0.0,
        torch.atan2(-sin_theta, -cos_theta),
        torch.atan2(sin_theta, cos_theta),
    )

    k_pos: torch.Tensor = two_theta / sin_theta
    k_neg: torch.Tensor = 2.0 * torch.ones_like(sin_theta)
    k: torch.Tensor = torch.where(sin_squared_theta > 0.0, k_pos, k_neg)

    angle_axis: torch.Tensor = torch.zeros_like(quaternion)[..., :3]
    angle_axis[..., 0] += q1 * k
    angle_axis[..., 1] += q2 * k
    angle_axis[..., 2] += q3 * k
    return angle_axis

@torch.jit.script
def rotation_matrix_to_quaternion(rotation_matrix, eps:float=1e-6):
    """
    This function is borrowed from https://github.com/kornia/kornia

    Convert 3x4 rotation matrix to 4d quaternion vector

    This algorithm is based on algorithm described in
    https://github.com/KieranWynn/pyquaternion/blob/master/pyquaternion/quaternion.py#L201

    Args:
        rotation_matrix (Tensor): the rotation matrix to convert.

    Return:
        Tensor: the rotation in quaternion

    Shape:
        - Input: :math:`(N, 3, 4)`
        - Output: :math:`(N, 4)`

    Example:
        >>> input = torch.rand(4, 3, 4)  # Nx3x4
        >>> output = tgm.rotation_matrix_to_quaternion(input)  # Nx4
    """
    # if not torch.is_tensor(rotation_matrix):
    #     raise TypeError(
    #         "Input type is not a torch.Tensor. Got {}".format(type(rotation_matrix))
    #     )

    if len(rotation_matrix.shape) > 3:
        raise ValueError(
            "Input size must be a three dimensional tensor. Got {}".format(
                rotation_matrix.shape
            )
        )
    if not rotation_matrix.shape[-2:] == (3, 4):
        raise ValueError(
            "Input size must be a N x 3 x 4  tensor. Got {}".format(
                rotation_matrix.shape
            )
        )

    rmat_t = torch.transpose(rotation_matrix, 1, 2)

    mask_d2 = rmat_t[:, 2, 2] < eps

    mask_d0_d1 = rmat_t[:, 0, 0] > rmat_t[:, 1, 1]
    mask_d0_nd1 = rmat_t[:, 0, 0] < -rmat_t[:, 1, 1]

    t0 = 1 + rmat_t[:, 0, 0] - rmat_t[:, 1, 1] - rmat_t[:, 2, 2]
    q0 = torch.stack(
        [
            rmat_t[:, 1, 2] - rmat_t[:, 2, 1],
            t0,
            rmat_t[:, 0, 1] + rmat_t[:, 1, 0],
            rmat_t[:, 2, 0] + rmat_t[:, 0, 2],
        ],
        -1,
    )
    t0_rep = t0.repeat(4, 1).t()

    t1 = 1 - rmat_t[:, 0, 0] + rmat_t[:, 1, 1] - rmat_t[:, 2, 2]
    q1 = torch.stack(
        [
            rmat_t[:, 2, 0] - rmat_t[:, 0, 2],
            rmat_t[:, 0, 1] + rmat_t[:, 1, 0],
            t1,
            rmat_t[:, 1, 2] + rmat_t[:, 2, 1],
        ],
        -1,
    )
    t1_rep = t1.repeat(4, 1).t()

    t2 = 1 - rmat_t[:, 0, 0] - rmat_t[:, 1, 1] + rmat_t[:, 2, 2]
    q2 = torch.stack(
        [
            rmat_t[:, 0, 1] - rmat_t[:, 1, 0],
            rmat_t[:, 2, 0] + rmat_t[:, 0, 2],
            rmat_t[:, 1, 2] + rmat_t[:, 2, 1],
            t2,
        ],
        -1,
    )
    t2_rep = t2.repeat(4, 1).t()

    t3 = 1 + rmat_t[:, 0, 0] + rmat_t[:, 1, 1] + rmat_t[:, 2, 2]
    q3 = torch.stack(
        [
            t3,
            rmat_t[:, 1, 2] - rmat_t[:, 2, 1],
            rmat_t[:, 2, 0] - rmat_t[:, 0, 2],
            rmat_t[:, 0, 1] - rmat_t[:, 1, 0],
        ],
        -1,
    )
    t3_rep = t3.repeat(4, 1).t()

    mask_c0 = mask_d2 * mask_d0_d1
    mask_c1 = mask_d2 * ~mask_d0_d1
    mask_c2 = ~mask_d2 * mask_d0_nd1
    mask_c3 = ~mask_d2 * ~mask_d0_nd1
    mask_c0 = mask_c0.view(-1, 1).type_as(q0)
    mask_c1 = mask_c1.view(-1, 1).type_as(q1)
    mask_c2 = mask_c2.view(-1, 1).type_as(q2)
    mask_c3 = mask_c3.view(-1, 1).type_as(q3)

    q = q0 * mask_c0 + q1 * mask_c1 + q2 * mask_c2 + q3 * mask_c3
    q /= torch.sqrt(
        t0_rep * mask_c0
        + t1_rep * mask_c1
        + t2_rep * mask_c2  # noqa
        + t3_rep * mask_c3
    )  # noqa
    q *= 0.5
    return q


@torch.jit.script
def rotation_matrix_to_angle_axis(rotation_matrix):
    """
    This function is borrowed from https://github.com/kornia/kornia

    Convert 3x4 rotation matrix to Rodrigues vector

    Args:
        rotation_matrix (Tensor): rotation matrix.

    Returns:
        Tensor: Rodrigues vector transformation.

    Shape:
        - Input: :math:`(N, 3, 4)`
        - Output: :math:`(N, 3)`

    Example:
        >>> input = torch.rand(2, 3, 4)  # Nx4x4
        >>> output = tgm.rotation_matrix_to_angle_axis(input)  # Nx3
    """
    if rotation_matrix.shape[1:] == (3, 3):
        rot_mat = rotation_matrix.reshape(-1, 3, 3)
        hom = (
            torch.tensor([0, 0, 1], dtype=torch.float32, device=rotation_matrix.device)
            .reshape(1, 3, 1)
            .expand(rot_mat.shape[0], -1, -1)
        )
        rotation_matrix = torch.cat([rot_mat, hom], dim=-1)

    quaternion = rotation_matrix_to_quaternion(rotation_matrix)
    aa = quaternion_to_angle_axis(quaternion)
    aa[torch.isnan(aa)] = 0.0
    return aa


def batch_euler2matrix(r):
    return quaternion_to_rotation_matrix(euler_to_quaternion(r))


def euler_to_quaternion(r):
    x = r[..., 0]
    y = r[..., 1]
    z = r[..., 2]

    z = z / 2.0
    y = y / 2.0
    x = x / 2.0
    cz = torch.cos(z)
    sz = torch.sin(z)
    cy = torch.cos(y)
    sy = torch.sin(y)
    cx = torch.cos(x)
    sx = torch.sin(x)
    quaternion = torch.zeros_like(r.repeat(1, 2))[..., :4].to(r.device)
    quaternion[..., 0] += cx * cy * cz - sx * sy * sz
    quaternion[..., 1] += cx * sy * sz + cy * cz * sx
    quaternion[..., 2] += cx * cz * sy - sx * cy * sz
    quaternion[..., 3] += cx * cy * sz + sx * cz * sy
    return quaternion

@torch.jit.script
def quaternion_to_rotation_matrix(quat):
    """Convert quaternion coefficients to rotation matrix.
    Args:
        quat: size = [B, 4] 4 <===>(w, x, y, z)
    Returns:
        Rotation matrix corresponding to the quaternion -- size = [B, 3, 3]
    """
    norm_quat = quat
    norm_quat = norm_quat / norm_quat.norm(p=2, dim=1, keepdim=True)
    w, x, y, z = norm_quat[:, 0], norm_quat[:, 1], norm_quat[:, 2], norm_quat[:, 3]

    B = quat.size(0)

    w2, x2, y2, z2 = w.pow(2), x.pow(2), y.pow(2), z.pow(2)
    wx, wy, wz = w * x, w * y, w * z
    xy, xz, yz = x * y, x * z, y * z

    rotMat = torch.stack(
        [
            w2 + x2 - y2 - z2,
            2 * xy - 2 * wz,
            2 * wy + 2 * xz,
            2 * wz + 2 * xy,
            w2 - x2 + y2 - z2,
            2 * yz - 2 * wx,
            2 * xz - 2 * wy,
            2 * wx + 2 * yz,
            w2 - x2 - y2 + z2,
        ],
        dim=1,
    ).view(B, 3, 3)
    return rotMat


def euler_angles_from_rotmat(R):
    """
    computer euler angles for rotation around x, y, z axis
    from rotation amtrix
    R: 4x4 rotation matrix
    https://www.gregslabaugh.net/publications/euler.pdf
    """
    r21 = np.round(R[:, 2, 0].item(), 4)
    if abs(r21) != 1:
        y_angle1 = -1 * torch.asin(R[:, 2, 0])
        y_angle2 = math.pi + torch.asin(R[:, 2, 0])
        cy1, cy2 = torch.cos(y_angle1), torch.cos(y_angle2)

        x_angle1 = torch.atan2(R[:, 2, 1] / cy1, R[:, 2, 2] / cy1)
        x_angle2 = torch.atan2(R[:, 2, 1] / cy2, R[:, 2, 2] / cy2)
        z_angle1 = torch.atan2(R[:, 1, 0] / cy1, R[:, 0, 0] / cy1)
        z_angle2 = torch.atan2(R[:, 1, 0] / cy2, R[:, 0, 0] / cy2)

        s1 = (x_angle1, y_angle1, z_angle1)
        s2 = (x_angle2, y_angle2, z_angle2)
        s = (s1, s2)

    else:
        z_angle = torch.tensor([0], device=R.device).float()
        if r21 == -1:
            y_angle = torch.tensor([math.pi / 2], device=R.device).float()
            x_angle = z_angle + torch.atan2(R[:, 0, 1], R[:, 0, 2])
        else:
            y_angle = -torch.tensor([math.pi / 2], device=R.device).float()
            x_angle = -z_angle + torch.atan2(-R[:, 0, 1], R[:, 0, 2])
        s = ((x_angle, y_angle, z_angle),)
    return s

@torch.jit.script
def quaternion_raw_multiply(a, b):
    """
    Source: https://github.com/facebookresearch/pytorch3d/blob/main/pytorch3d/transforms/rotation_conversions.py
    Multiply two quaternions.
    Usual torch rules for broadcasting apply.

    Args:
        a: Quaternions as tensor of shape (..., 4), real part first.
        b: Quaternions as tensor of shape (..., 4), real part first.

    Returns:
        The product of a and b, a tensor of quaternions shape (..., 4).
    """
    aw, ax, ay, az = torch.unbind(a, -1)
    bw, bx, by, bz = torch.unbind(b, -1)
    ow = aw * bw - ax * bx - ay * by - az * bz
    ox = aw * bx + ax * bw + ay * bz - az * by
    oy = aw * by - ax * bz + ay * bw + az * bx
    oz = aw * bz + ax * by - ay * bx + az * bw
    return torch.stack((ow, ox, oy, oz), -1)

@torch.jit.script
def quaternion_invert(quaternion):
    """
    Source: https://github.com/facebookresearch/pytorch3d/blob/main/pytorch3d/transforms/rotation_conversions.py
    Given a quaternion representing rotation, get the quaternion representing
    its inverse.

    Args:
        quaternion: Quaternions as tensor of shape (..., 4), with real part
            first, which must be versors (unit quaternions).

    Returns:
        The inverse, a tensor of quaternions of shape (..., 4).
    """

    return quaternion * torch.tensor([1.0, -1.0, -1.0, -1.0], device=quaternion.device, dtype=quaternion.dtype)


@torch.jit.script
def quaternion_apply(quaternion, point):
    """
    Source: https://github.com/facebookresearch/pytorch3d/blob/main/pytorch3d/transforms/rotation_conversions.py
    Apply the rotation given by a quaternion to a 3D point.
    Usual torch rules for broadcasting apply.

    Args:
        quaternion: Tensor of quaternions, real part first, of shape (..., 4).
        point: Tensor of 3D points of shape (..., 3).

    Returns:
        Tensor of rotated points of shape (..., 3).
    """
    if point.size(-1) != 3:
        raise ValueError(f"Points are not in 3D, f{point.shape}.")
    real_parts = point.new_zeros(point.shape[:-1] + (1,))
    point_as_quaternion = torch.cat((real_parts, point), -1)
    out = quaternion_raw_multiply(
        quaternion_raw_multiply(quaternion, point_as_quaternion),
        quaternion_invert(quaternion),
    )
    return out[..., 1:]

@torch.jit.script
def axis_angle_to_quaternion(axis_angle: torch.Tensor) -> torch.Tensor:
    """
    Source: https://github.com/facebookresearch/pytorch3d/blob/main/pytorch3d/transforms/rotation_conversions.py
    Convert rotations given as axis/angle to quaternions.
    Args:
        axis_angle: Rotations given as a vector in axis angle form,
            as a tensor of shape (..., 3), where the magnitude is
            the angle turned anticlockwise in radians around the
            vector's direction.
    Returns:
        quaternions with real part first, as tensor of shape (..., 4).
    """
    angles = torch.norm(axis_angle, p=2, dim=-1, keepdim=True)
    half_angles = angles * 0.5
    eps = 1e-6
    small_angles = angles.abs() < eps
    sin_half_angles_over_angles = torch.empty_like(angles)
    sin_half_angles_over_angles[~small_angles] = (
        torch.sin(half_angles[~small_angles]) / angles[~small_angles]
    )
    # for x small, sin(x/2) is about x/2 - (x/2)^3/6
    # so sin(x/2)/x is about 1/2 - (x*x)/48
    sin_half_angles_over_angles[small_angles] = (
        0.5 - (angles[small_angles] * angles[small_angles]) / 48
    )
    quaternions = torch.cat(
        [torch.cos(half_angles), axis_angle * sin_half_angles_over_angles], dim=-1
    )
    return quaternions

@torch.jit.script
def rot6d_to_aa(x: torch.Tensor) -> torch.Tensor:
    """
    Source:
    
    Convert 6D rotation representation to axis/angle.
    Args:
        x: Rotations given as a vector in 6D form, as a tensor of shape (..., 6).
    Returns:
        Rotations given as a vector in axis/angle form, as a tensor of shape (..., 3), where the magnitude is the angle turned anticlockwise in radians around the vector's direction.
    """
    return rotation_matrix_to_angle_axis(rot6d_to_rotmat(x))

@torch.jit.script
def aa_to_rot6d(x: torch.Tensor) -> torch.Tensor:
    """
    Source:
    
    Convert axis/angle to 6D rotation representation.
    Args:
        x: Rotations given as a vector in axis/angle form, as a tensor of shape (..., 3).
    Returns:
        Rotations given as a vector in 6D form, as a tensor of shape (..., 6).
    """
    return rotmat_to_rot6d(batch_rodrigues(x))



@torch.jit.script
def get_pose12d_from_world_to_camera(rotmat_world: torch.Tensor, trans_world: torch.Tensor, extrinsics: torch.Tensor):
    """
    Transform poses from the world frame to the camera frame using the extrinsics matrix.

    Args:
        rotmat_world: (T, 3, 3) rotation matrix in world frame
        trans_world: (T, 3) translation in world frame
        extrinsics: (4, 4) or (T, 4, 4) tensor of world2camera matrices.

    Returns:
        R_cam: (T, 3, 3) rotation matrix in camera frame
        trans_cam: (T, 3) translation in camera frame
    """
    # Ensure extrinsics is (T, 4, 4)
    if extrinsics.dim() == 2:
        extrinsics = extrinsics.unsqueeze(0)  # Add batch dimension if necessary

    # Extract rotation and translation from extrinsics
    R_world2cam = extrinsics[:, :3, :3]  # (T, 3, 3)
    t_world2cam = extrinsics[:, :3, 3]   # (T, 3)

    # Transform rotation: R_cam = R_world2cam @ R_world
    R_cam = torch.bmm(R_world2cam, rotmat_world)  # (T, 3, 3)

    # Transform translation: t_cam = R_world2cam @ t_world + t_world2cam
    trans_cam = torch.bmm(R_world2cam, trans_world.unsqueeze(-1)).squeeze(-1) + t_world2cam  # (T, 3)

    return R_cam, trans_cam

@torch.jit.script
def get_pose12d_from_camera_to_world(rotmat_cam: torch.Tensor, trans_cam: torch.Tensor, extrinsics: torch.Tensor):
    """
    Transform poses from the camera frame to the world frame using the extrinsics matrix.

    Args:
        rotmat_cam: (T, 3, 3) rotation matrix in camera frame
        trans_cam: (T, 3) translation in camera frame
        extrinsics: (4, 4) or (T, 4, 4) tensor of world2camera matrices.

    Returns:
        R_world: (T, 3, 3) rotation matrix in world frame
        trans_world: (T, 3) translation in world frame
    """
    # Ensure extrinsics is (T, 4, 4)
    if extrinsics.dim() == 2:
        extrinsics = extrinsics.unsqueeze(0)  # Add batch dimension if necessary

    # Compute the inverse of the extrinsics matrix (camera2world)
    R_cam2world = extrinsics[:, :3, :3].permute(0, 2, 1)  # (T, 3, 3)
    t_cam2world = -torch.bmm(R_cam2world, extrinsics[:, :3, 3].unsqueeze(-1)).squeeze(-1)  # (T, 3)

    # Transform rotation: R_world = R_cam2world @ R_cam
    R_world = torch.bmm(R_cam2world, rotmat_cam)  # (T, 3, 3)

    # Transform translation: t_world = R_cam2world @ t_cam + t_cam2world
    trans_world = torch.bmm(R_cam2world, trans_cam.unsqueeze(-1)).squeeze(-1) + t_cam2world  # (T, 3)

    return R_world, trans_world

@torch.jit.script
def compute_relative_pose_in_sequence(rotmat: torch.Tensor, translation: torch.Tensor):
    """
    Compute the relative pose sequence wrt. the first frame

    Args:
        rotmat (torch.Tensor): A tensor of shape (T+1, 3, 3) representing the rotation matrices.
        translation (torch.Tensor): A tensor of shape (T+1, 3) representing the translations.

    Returns:
        relative_rotmat (torch.Tensor): A tensor of shape (T, 3, 3) representing the relative rotation matrices to the first frame
        relative_translation (torch.Tensor): A tensor of shape (T, 3) representing the relative translations to the first frame
    """
    ref_rotmat_inv = rotmat[0].T.unsqueeze(0)  # (1, 3, 3)
    ref_translation = translation[0]  # (3,)
    # Compute relative rotation and translation for each pose in the sequence
    relative_rotmat = torch.matmul(ref_rotmat_inv, rotmat[1:])  # (T, 3, 3)
    relative_translation = torch.matmul(ref_rotmat_inv, (translation[1:] - ref_translation).unsqueeze(-1)).squeeze(-1)  # (T, 3)
    return relative_rotmat, relative_translation

@torch.jit.script
def decode_absolute_pose_from_relative(ref_rotmat: torch.Tensor, ref_translation: torch.Tensor, rel_rotmat: torch.Tensor, rel_translation: torch.Tensor):
    """
    Compute the absolute pose sequence wrt. the initial absolute reference frame

    Args:
        ref_rotmat (torch.Tensor): A tensor of shape (1, 3, 3) representing the rotation matrices.
        ref_translation (torch.Tensor): A tensor of shape (1, 3) representing the translations.
        rel_rotmat (torch.Tensor): A tensor of shape (T, 3, 3) representing the rotation matrices.
        rel_translation (torch.Tensor): A tensor of shape (T, 3) representing the translations.

    Returns:
        abs_rotmat (torch.Tensor): A tensor of shape (T, 3, 3) representing the absolute rotation matrices to the reference frame
        abs_translation (torch.Tensor): A tensor of shape (T, 3) representing the absolute translations to the reference frame
    """
    T = rel_rotmat.size(0)

    # Expand ref frame to (T, 3, 3) and (T, 3) for broadcasted matmul
    ref_rotmat_exp = ref_rotmat.expand(T, -1, -1)        # (T, 3, 3)
    ref_translation_exp = ref_translation.expand(T, -1)  # (T, 3)

    # Absolute rotation and translation
    abs_rotmat = torch.bmm(ref_rotmat_exp, rel_rotmat)   # (T, 3, 3)
    abs_translation = torch.bmm(ref_rotmat_exp, rel_translation.unsqueeze(-1)).squeeze(-1) + ref_translation_exp  # (T, 3)
    return abs_rotmat, abs_translation

@torch.jit.script
def rotmat_dist(current, desired):
    '''
    current: (num_envs, 3, 3)
    desired: (num_envs, 3, 3)
    return: (num_envs,) [0,pi]
    '''
    return torch.acos(
        torch.clamp(
            (torch.diagonal(desired.transpose(-1, -2) @ current, dim1=-2, dim2=-1).sum(-1) - 1) / 2,
            -1,
            1,
        )
    )

