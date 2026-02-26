from __future__ import annotations

from typing import Iterable, Optional, Tuple, Union

from PySide6 import QtCore, QtGui


PointLike = Union[QtCore.QPoint, QtCore.QPointF, Tuple[float, float], Tuple[int, int], Iterable[float]]
RectTuple = Tuple[int, int, int, int]


def _screen_device_pixel_ratio(screen: QtGui.QScreen) -> float:
    try:
        dpr = float(screen.devicePixelRatio())
    except Exception:
        dpr = 1.0
    if dpr <= 0 or not float(dpr):
        return 1.0
    return dpr


def _screen_native_geometry(screen: QtGui.QScreen) -> QtCore.QRect:
    native_geo = None
    try:
        native_geo_attr = getattr(screen, "nativeGeometry", None)
        if callable(native_geo_attr):
            native_geo = native_geo_attr()
    except Exception:
        native_geo = None
    if isinstance(native_geo, QtCore.QRect):
        return native_geo

    geo = QtCore.QRect(screen.geometry())
    dpr = _screen_device_pixel_ratio(screen)
    if abs(dpr - 1.0) < 1e-6:
        return geo
    return QtCore.QRect(
        int(round(geo.x() * dpr)),
        int(round(geo.y() * dpr)),
        int(round(geo.width() * dpr)),
        int(round(geo.height() * dpr)),
    )


def _as_pointf(value: PointLike) -> QtCore.QPointF:
    if isinstance(value, QtCore.QPointF):
        return QtCore.QPointF(value)
    if isinstance(value, QtCore.QPoint):
        return QtCore.QPointF(value)
    try:
        x, y = value  # type: ignore[misc]
        return QtCore.QPointF(float(x), float(y))
    except Exception:
        return QtCore.QPointF(0.0, 0.0)


def _screen_for_logical_point(pt: QtCore.QPointF) -> Optional[QtGui.QScreen]:
    qpoint = QtCore.QPoint(int(round(pt.x())), int(round(pt.y())))
    screen = QtGui.QGuiApplication.screenAt(qpoint)
    if screen is not None:
        return screen
    return QtGui.QGuiApplication.primaryScreen()


def _screen_for_native_point(pt: QtCore.QPointF) -> Optional[QtGui.QScreen]:
    qpoint = QtCore.QPoint(int(round(pt.x())), int(round(pt.y())))
    for screen in QtGui.QGuiApplication.screens():
        native_geo = _screen_native_geometry(screen)
        if native_geo.contains(qpoint):
            return screen
    return QtGui.QGuiApplication.primaryScreen()


def logical_point_to_native(point: PointLike) -> QtCore.QPoint:
    pt = _as_pointf(point)
    screen = _screen_for_logical_point(pt)
    if screen is None:
        return QtCore.QPoint(int(round(pt.x())), int(round(pt.y())))

    geo = screen.geometry()
    native_geo = _screen_native_geometry(screen)
    if geo.width() <= 0 or geo.height() <= 0:
        return QtCore.QPoint(int(round(pt.x())), int(round(pt.y())))

    rel_x = pt.x() - geo.x()
    rel_y = pt.y() - geo.y()

    scale_x = native_geo.width() / max(1.0, float(geo.width()))
    scale_y = native_geo.height() / max(1.0, float(geo.height()))

    native_x = native_geo.x() + rel_x * scale_x
    native_y = native_geo.y() + rel_y * scale_y
    return QtCore.QPoint(int(round(native_x)), int(round(native_y)))


def native_point_to_logical(point: PointLike) -> QtCore.QPointF:
    pt = _as_pointf(point)
    screen = _screen_for_native_point(pt)
    if screen is None:
        return QtCore.QPointF(pt)

    geo = screen.geometry()
    native_geo = _screen_native_geometry(screen)
    if native_geo.width() <= 0 or native_geo.height() <= 0:
        return QtCore.QPointF(pt)

    rel_x = pt.x() - native_geo.x()
    rel_y = pt.y() - native_geo.y()

    inv_scale_x = geo.width() / max(1.0, float(native_geo.width()))
    inv_scale_y = geo.height() / max(1.0, float(native_geo.height()))

    logical_x = geo.x() + rel_x * inv_scale_x
    logical_y = geo.y() + rel_y * inv_scale_y
    return QtCore.QPointF(logical_x, logical_y)


def logical_rect_to_native(rect: Union[QtCore.QRect, QtCore.QRectF]) -> QtCore.QRect:
    r = QtCore.QRectF(rect)
    top_left = logical_point_to_native(r.topLeft())
    bottom_right_logical = QtCore.QPointF(r.left() + r.width(), r.top() + r.height())
    bottom_right = logical_point_to_native(bottom_right_logical)
    native = QtCore.QRect(top_left, bottom_right)
    native = native.normalized()
    return native


def native_rect_tuple_to_logical(rect: RectTuple) -> QtCore.QRectF:
    left, top, right, bottom = rect
    top_left = native_point_to_logical((left, top))
    bottom_right = native_point_to_logical((right, bottom))
    size = QtCore.QSizeF(bottom_right.x() - top_left.x(), bottom_right.y() - top_left.y())
    logical = QtCore.QRectF(top_left, size)
    return logical.normalized()
