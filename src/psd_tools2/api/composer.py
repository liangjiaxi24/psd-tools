"""
Composer module.
"""
from __future__ import absolute_import, unicode_literals
import logging

from psd_tools2.api.pil_io import get_pil_mode, convert_pattern_to_pil

logger = logging.getLogger(__name__)


def extract_bbox(layers):
    """
    Returns a bounding box for ``layers`` or (0, 0, 0, 0) if the layers
    have no bounding box.
    """
    if not hasattr(layers, '__iter__'):
        layers = [layers]
    bboxes = [
        layer.bbox for layer in layers
        if layer.is_visible() and not layer.bbox == (0, 0, 0, 0)
    ]
    if len(bboxes) == 0:  # Empty bounding box.
        return (0, 0, 0, 0)
    lefts, tops, rights, bottoms = zip(*bboxes)
    return (min(lefts), min(tops), max(rights), max(bottoms))


def intersect(*bboxes):
    if len(bboxes) == 0:
        return (0, 0, 0, 0)

    lefts, tops, rights, bottoms = zip(*bboxes)
    result = (max(lefts), max(tops), min(rights), min(bottoms))
    if result[2] <= result[0] or result[3] <= result[1]:
        return (0, 0, 0, 0)

    return result


def _blend(target, image, offset, mask=None):
    if offset[0] < 0:
        if image.width <= -offset[0]:
            return target
        image = image.crop((-offset[0], 0, image.width, image.height))
        offset = (0, offset[1])

    if offset[1] < 0:
        if image.height <= -offset[1]:
            return target
        image = image.crop((0, -offset[1], image.width, image.height))
        offset = (offset[0], 0)

    if target.mode == 'RGBA':
        target.alpha_composite(image.convert('RGBA'), offset)
    else:
        tmp = target.convert('RGBA')
        tmp.alpha_composite(image.convert('RGBA'), offset)
        target = tmp.convert(target.mode)
    return target


def compose(layers, bbox=None, layer_filter=None, color=None):
    """
    Compose layers to a single ``PIL.Image``.

    In order to skip some layers, pass ``layer_filter`` function which
    should take ``layer`` as an argument and return True to keep the layer
    or return False to skip. By default, layers that satisfies the following
    condition is composed::

        layer.is_visible()

    Currently the following are ignored:

     - Clipping layers.
     - Layers that do not have associated pixels in the file.
     - Adjustments layers.
     - Layer effects.
     - Blending mode (all blending modes become normal).

    This function is experimental and does not guarantee Photoshop-quality
    rendering.

    :param layers: a layer, or an iterable of layers.
    :param bbox: (left, top, bottom, right) tuple that specifies a region to
        compose. By default, all the visible area is composed. The origin
        is at the top-left corner of the PSD document.
    :param layer_filter: a callable that takes a layer and returns bool.
    :param color: background color in int or tuple.
    :return: PIL Image or None.
    """
    from PIL import Image

    if not hasattr(layers, '__iter__'):
        layers = [layers]

    def _default_filter(layer):
        return layer.is_visible()

    layer_filter = layer_filter or _default_filter
    valid_layers = [x for x in layers if layer_filter(x)]
    if len(valid_layers) == 0:
        return None

    if bbox is None:
        bbox = extract_bbox(valid_layers)
        if bbox == (0, 0, 0, 0):
            return None

    # Alpha must be forced to correctly blend.
    mode = get_pil_mode(valid_layers[0]._psd.header.color_mode, True)
    result = Image.new(
        mode, (bbox[2] - bbox[0], bbox[3] - bbox[1]), color=color,
    )

    initial_layer = True
    for layer in valid_layers:
        if intersect(layer.bbox, bbox) == (0, 0, 0, 0):
            continue

        image = layer.compose()
        if image is None:
            continue

        logger.debug('Composing %s' % layer)
        offset = (layer.left - bbox[0], layer.top - bbox[1])
        if initial_layer:
            result.paste(image, offset)
            initial_layer = False
        else:
            result = _blend(result, image, offset)

    return result


def compose_layer(layer):
    """Compose a single layer with pixels."""
    from PIL import Image

    if layer.bbox == (0, 0, 0, 0):
        return None

    image = None
    if layer.has_pixels():
        image = layer.topil()
    elif 'SOLID_COLOR_SHEET_SETTING' in layer.tagged_blocks:
        image = draw_solid_color_fill(layer)
    elif 'PATTERN_FILL_SETTING' in layer.tagged_blocks:
        image = draw_pattern_fill(layer)
    elif 'GRADIENT_FILL_SETTING' in layer.tagged_blocks:
        image = draw_gradient_fill(layer)

    if image is None:
        return image

    # Apply mask.
    if layer.has_mask() and not layer.mask.disabled:
        mask_bbox = layer.mask.bbox
        if mask_bbox != (0, 0, 0, 0):
            color = layer.mask.background_color
            offset = (mask_bbox[0] - layer.left, mask_bbox[1] - layer.top)
            mask = Image.new('L', image.size, color=color)
            mask.paste(layer.mask.topil(), offset)
            if image.mode.endswith('A'):
                # What should we do here? There are two alpha channels.
                pass
            image.putalpha(mask)
    elif layer.has_vector_mask():
        mask = draw_vector_mask(layer)
        image.putalpha(mask)


    # Clip layers.
    # if layer.has_clip_layers():
    #     clip_box = extract_bbox(layer.clip_layers)
    #     if clip_box != (0, 0, 0, 0):
    #         clip_image = compose(layer.clip_layers, bbox=clip_box)


    # Apply opacity.
    if layer.opacity < 255:
        opacity = int(
            layer.tagged_blocks.get_data('BLEND_FILL_OPACITY', 1) *
            layer.opacity
        )
        if image.mode.endswith('A'):
            opacity = opacity / 255.
            channels = list(image.split())
            channels[-1] = channels[-1].point(lambda x: int(x * opacity))
            image = Image.merge(image.mode, channels)
        else:
            image.putalpha(opacity)

    return image


def draw_vector_mask(layer):
    from PIL import Image, ImageDraw

    width = layer._psd.header.width
    height = layer._psd.header.height
    color = layer.vector_mask.initial_fill_rule * 255
    mask = Image.new('L', (width, height), color)
    draw = ImageDraw.Draw(mask)
    for subpath in layer.vector_mask.paths:
        path = [(
            int(knot.anchor[1] * width),
            int(knot.anchor[0] * height),
        ) for knot in subpath]
        # TODO: Use bezier curve instead of polygon.
        draw.polygon(path, fill=(255 - color))
    del draw

    return mask.crop(layer.bbox)


def draw_solid_color_fill(layer):
    from PIL import Image

    mode = get_pil_mode(layer._psd.header.color_mode, True)
    fill = layer.tagged_blocks.get_data('SOLID_COLOR_SHEET_SETTING')
    color = tuple(int(x) for x in fill.get(b'Clr ').values())
    return Image.new(mode, (layer.width, layer.height), color)


def draw_pattern_fill(layer):
    from PIL import Image

    mode = get_pil_mode(layer._psd.header.color_mode, True)
    fill = layer.tagged_blocks.get_data('PATTERN_FILL_SETTING')
    pattern_id = fill[b'Ptrn'][b'Idnt'].value.rstrip('\x00')
    pattern = _get_pattern(layer._psd, pattern_id)
    if not pattern:
        logger.error('Pattern not found: %s' % (pattern_id))
        return None
    panel = convert_pattern_to_pil(pattern, layer._psd.header.version)
    image = Image.new(mode, (layer.width, layer.height))
    for left in range(0, image.width, panel.width):
        for top in range(0, image.height, panel.height):
            image.paste(panel, (left, top))
    return image


def draw_gradient_fill(layer):
    from PIL import Image
    logger.debug('Not implemented yet')
    return None


def _get_pattern(psd, pattern_id):
    tagged_blocks = psd.layer_and_mask_information.tagged_blocks
    for key in ('PATTERNS1', 'PATTERNS2', 'PATTERNS3'):
        if key in tagged_blocks:
            data = tagged_blocks.get_data(key)
            for pattern in data:
                if pattern.pattern_id == pattern_id:
                    return pattern
    return None
