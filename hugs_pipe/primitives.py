from __future__ import division, print_function

import numpy as np
import lsst.afw.image as afwImage
import lsst.afw.detection as afwDet
from astropy.table import Table, vstack
from astropy.convolution import Gaussian2DKernel
import photutils as phut
from . import utils

__all__ = ['associate', 'deblend_stamps', 'image_threshold', 'photometry']


def associate(mask, fpset, r_in=5, r_out=15, max_on_bit=20., 
              min_pix=1, plane_name='THRESH_HIGH', dilate=None):
    """
    Associate footprints in fpset with footprints in mask plane 
    'plane_name'. A footprint is associated with an object if 
    an on bit falls within an annulus centered with respect to 
    all its peaks.
    

    Parameters
    ----------
    mask : lsst.afw.image.imageLib.MaskU
        Mask object with plane named 'plane_name'.
    fpset : lsst.afw.detection.detectionLib.FootprintSet
        Set of footprints to associate with objects in mask.
    r_in : float, optional
        Inner radius in pixels of the association annulus.
    r_out : float, optional
        Outer radius in pixels of the association annulus.
    max_on_bit : int, optional
        Maximum number of on bits to consider as associated.
    min_pix : int, optional
        Footprints with less pixels will be masked. Set to 1 
        to ignore this option.
    plane_name : string, optional
        Name of the bit plane in mask to associate footprints with. 
    dilate : int, optional
        If not None, dilate association image with array of shape 
        (dilate, dilate). 

    Returns
    -------
    seg_assoc : 2D ndarray
        Segmentation image with non-zero values for all footprints 
        that are associated with an object in the mask. 

    Notes
    -----
    seg_assoc also includes footprints near objects with the 
    'BRIGHT_OBJECT' bit set.
    """
    x0, y0 = mask.getXY0()
    # False --> use footprint ids
    seg = fpset.insertIntoImage(False).getArray().copy()
    shape = seg.shape
    seg_assoc = np.zeros(shape, dtype=int)
    for foot in fpset.getFootprints():
        peaks = np.array([[p.getCentroid()[0]-x0, 
                           p.getCentroid()[1]-y0] for p in foot.getPeaks()])
        xc, yc = peaks.mean(axis=0)
        rows, cols = utils.annuli(yc, xc, r_in, r_out, shape=shape)
        ann_pix = mask.getArray()[rows, cols]
        on_bits = (ann_pix & mask.getPlaneBitMask(plane_name))!=0
        on_bits |= (ann_pix & mask.getPlaneBitMask('BRIGHT_OBJECT'))!=0
        if np.sum(on_bits) > max_on_bit:
            seg_assoc[seg==foot.getId()] = 1
        elif foot.getNpix() < min_pix:
            seg_assoc[seg==foot.getId()] = 1
    if dilate is not None:
        from scipy import ndimage
        dilator = np.ones((dilate, dilate))
        seg_assoc = ndimage.binary_dilation(seg_assoc, dilator)
        seg_assoc = seg_assoc.astype(int)
    return seg_assoc


def deblend_stamps(exposure, npix=5, thresh_snr=0.5, kern_sig_pix=3, 
                   detect_kwargs={}, deblend_kwargs={}):
    """
    Use photutils to deblend sources within "detection" postage stamps.

    Parameters
    ----------
    exposure : lsst.afw.ExposureF
        Exposure object with masks from hugsPipe.run.
    thresh_snr : float, optional
        The signal-to-noise ratio per pixel above the background 
        for which to consider a pixel as possibly being part of a source.
    kern_sig_pix : float
        Sigma (in pixels) of Gaussian kernal used to smooth detection image.
    npix : int, optional
        Number of pixels above required to be an object.
    detect_kwargs :  dict, optional
        Kwargs for photutils.detect_sources.
    deblend_kwargs :  dict, optional
        Kwargs for photutils.deblend_sources.

    Returns
    -------
    table : astropy.table.Table
        Source measurements. 
    """
    mask = exposure.getMaskedImage().getMask()
    if 'DETECTED_NEGATIVE' in list(mask.getMaskPlaneDict().keys()):
        mask.removeAndClearMaskPlane('DETECTED_NEGATIVE', True)
    planes = mask.getPlaneBitMask(['THRESH_LOW', 'DETECTED'])
    fpset = afwDet.FootprintSet(
        mask, afwDet.Threshold(planes, afwDet.Threshold.BITMASK))
    wcs = exposure.getWcs()
    table = Table()
    kern = Gaussian2DKernel(kern_sig_pix)
    kern.normalize()
    fp_id = 1
    for fp in fpset.getFootprints():
        bbox = fp.getBBox()
        exp = exposure.Factory(exposure, bbox, afwImage.PARENT)
        hfp = afwDet.HeavyFootprintF(fp, exposure.getMaskedImage())
        pix = hfp.getMaskArray()
        bits = [(pix & mask.getPlaneBitMask(['DETECTED'])!=0).sum()>0,
                (pix & mask.getPlaneBitMask(['BRIGHT_OBJECT'])!=0).sum()==0,
                (pix & mask.getPlaneBitMask(['THRESH_LOW'])!=0).sum()>0,
                (pix & mask.getPlaneBitMask(['THRESH_HIGH'])!=0).sum()<10]
        if np.alltrue(bits):
            img = exp.getMaskedImage().getImage().getArray().copy()
            x0, y0 = exp.getXY0()
            thresh = phut.detect_threshold(img, 
                                           snr=thresh_snr, 
                                           **detect_kwargs)
            seg = phut.detect_sources(img, thresh, npixels=npix, 
                                      filter_kernel=kern)
            if seg.nlabels==0: 
                continue
            seg_db = phut.deblend_sources(
                img, seg, npixels=npix, 
                filter_kernel=kern, **deblend_kwargs)
            props = phut.source_properties(img, seg_db)
            props = phut.properties_table(props)
            props['x_hsc'] = props['xcentroid'] + x0
            props['y_hsc'] = props['ycentroid'] + y0
            props['fp_id'] = [fp_id]*len(props)
            fp_id += 1
            if len(props)>1:
                props['is_deblended'] = [True]*len(props)
            else:
                props['is_deblended'] = False
            table = vstack([table, props])
    table['id'] = np.arange(1, len(table)+1)
    table.remove_column('ra_icrs_centroid')
    table.remove_column('dec_icrs_centroid')
    ra_list = []
    dec_list = []
    for x, y in table['x_hsc', 'y_hsc']:
        ra = wcs.pixelToSky(x, y).getLongitude().asDegrees()
        dec = wcs.pixelToSky(x, y).getLatitude().asDegrees()
        ra_list.append(ra)
        dec_list.append(dec)
    table['ra'] = ra_list
    table['dec'] = dec_list
    table['x_img'] = table['x_hsc'] - exposure.getX0() 
    table['y_img'] = table['y_hsc'] - exposure.getY0() 
    return table


def image_threshold(masked_image, thresh=3.0, thresh_type='stdev', npix=1, 
                    rgrow=None, isogrow=False, plane_name='', mask=None,
                    clear_mask=True):
    """
    Image thresholding. A bit mask will be set with name 'plane_name'.

    Parameters
    ----------
    masked_image : lsst.afw.image.imageLib.MaskedImageF
        A masked image object.
    thresh : float
        Threshold value.
    thresh_type : string, optional
        Threshold type: stdev, pixel_stdev, bitmask, value,
        or variance.
    npix : int, optional
        Minimum number of touching pixels in an object.
    rgrow : int, optional
        Number of pixels to grow footprints.
    isogrow : bool, optional
        If True, use (expensive) isotropic grow. 
    plane_name : string, optional
        Name of bit plane.
    mask : lsst.afw.image.imageLib.MaskU, optional
        Mask to set if not same as in masked_imaged
    clear_mask : bool, optional
        If True, clear the bit plane before thresholding

    Returns
    -------
    fpset : lsst.afw.detection.detectionLib.FootprintSet
        Footprints associated with detected objects.
    """
    mask = masked_image.getMask() if mask is None else mask
    thresh_type = getattr(afwDet.Threshold, thresh_type.upper())
    thresh = afwDet.Threshold(thresh, thresh_type)
    fpset = afwDet.FootprintSet(masked_image, thresh, '', npix)
    if rgrow is not None:
        fpset = afwDet.FootprintSet(fpset, rgrow, isogrow)
    if plane_name:
        mask.addMaskPlane(plane_name)
        if clear_mask:
            mask.clearMaskPlane(mask.getMaskPlane(plane_name))
        fpset.setMask(mask, plane_name)
    return fpset


def photometry(img, sources, zpt_mag=27.0, ell_nsig=4.0, 
               circ_radii=[3, 6, 9]):
    """
    Do basic aperture photometry within cicular apertures and 
    an elliptical aperture. Results will be saved to the 
    input table.

    Parameters
    ----------
    img : 2D ndarray
        The image data.
    sources : astropy.table.Table
        Output table from deblend_stamps.
    zpt_mag : float, optional
        The zero point magnitude.
    ell_nsig : float, optional
        Number be sigma for major/minor axis sizes.
    circ_radii : list, optional
        Radii within which to calculate surface brightness.
    """

    pos = [(x, y) for x,y in sources['x_img', 'y_img']] 

    mag = []
    for idx in range(len(sources)):
        x, y = pos[idx]
        a, b = sources['semimajor_axis_sigma', 'semiminor_axis_sigma'][idx]
        theta = sources['orientation'][idx]
        aperture = phut.EllipticalAperture((x,y), ell_nsig*a, ell_nsig*b, theta)
        flux = phut.aperture_photometry(img, aperture)['aperture_sum'][0]
        mag.append(zpt_mag - 2.5*np.log10(flux))
    sources['mag_ell'] = mag

    for r in circ_radii:
        r_arcsec = r*utils.pixscale
        apertures = phut.CircularAperture(pos, r=r)
        flux = phut.aperture_photometry(img, apertures)['aperture_sum']
        mag = zpt_mag - 2.5*np.log10(flux)
        sources['mag_circ_{}'.format(r)] = mag
        mu = mag + 2.5*np.log10(np.pi*r_arcsec**2)
        sources['mu_{}'.format(r)] = mu