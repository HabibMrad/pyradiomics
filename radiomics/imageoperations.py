from __future__ import print_function

from itertools import chain
import logging

import numpy
import pywt
import SimpleITK as sitk
import six
from six.moves import range

logger = logging.getLogger(__name__)


def getHistogram(binwidth, parameterValues):
  """
  Calculate and return the histogram using parameterValues (1D array of all segmented voxels in the image). Parameter
  binWidth determines the fixed width of each bin. This ensures comparable voxels after binning, a fixed bin count would
  be dependent on the intensity range in the segmentation. Returns a tuple of two elements: 1) histogram, a list where
  the n:sup:`th` element is the number of voxels assigned to the n:sup:`th` bin and 2) bin edges, a list of the edges of
  the calculated bins, length is N(bins) + 1.
  """
  global logger

  # Start binning form the first value lesser than or equal to the minimum value and evenly dividable by binwidth
  lowBound = min(parameterValues) - (min(parameterValues) % binwidth)
  # Add + binwidth to ensure the maximum value is included in the range generated by numpu.arange
  highBound = max(parameterValues) + binwidth

  binedges = numpy.arange(lowBound, highBound, binwidth)

  if len(binedges) == 1:  # Flat region, ensure that there is 1 bin
    binedges = 1

  logger.debug("Calculated %d bins for bin width %g with edges: %s)", len(binedges) - 1, binwidth, binedges)

  return numpy.histogram(parameterValues, bins=binedges)


def binImage(binwidth, parameterMatrix, parameterMatrixCoordinates):
  """
  Discretizes the parameterMatrix (matrix representation of the gray levels in the image) using the histogram calculated
  using :py:func:`getHistogram`. 1 is added to the upper edge of the last bin to ensure the maximum value is included in
  the top bin (due to the use of numpy.digitize, this could otherwise be topbin + 1). Only voxels defined by
  parameterMatrixCoordinates (defining the segmentatino) are used for calculation of histogram and subsequently
  discretized. Voxels outside segmentation are left unchanged.
  """
  global logger
  logger.debug('Discretizing gray levels inside ROI')

  histogram = getHistogram(binwidth, parameterMatrix[parameterMatrixCoordinates])

  histogram[1][-1] += 1  # ensures that max(self.targertVoxelArray) is binned to upper bin by numpy.digitize

  parameterMatrix[parameterMatrixCoordinates] = numpy.digitize(parameterMatrix[parameterMatrixCoordinates],
                                                               histogram[1])

  return parameterMatrix, histogram


def generateAngles(size, maxDistance=1):
  """
  Generate all possible angles from distance 1 until maxDistance in 3D.
  E.g. for d = 1, 13 angles are generated (representing the 26-connected region).
  For d = 2, 13 + 49 = 62 angles are generated (representing the 26 connected region for distance 1, and the 98
  connected region for distance 2)

  Impossible angles (where 'neighbouring' voxels will always be outside delineation) are deleted.

  :param size: dimensions (z, x, y) of the bounding box of the tumor mask.
  :param maxDistance: [1] Maximum distance between center voxel and neighbour
  :return: numpy array with shape (N, 3), where N is the number of unique angles
  """
  global logger

  logger.debug("Generating angles")

  angles = []

  for z in range(1, maxDistance + 1):
    angles.append((0, 0, z))
    for y in range(-maxDistance, maxDistance + 1):
      angles.append((0, z, y))
      for x in range(-maxDistance, maxDistance + 1):
        angles.append((z, y, x))

  angles = numpy.array(angles)

  angles = numpy.delete(angles, numpy.where(numpy.min(size - numpy.abs(angles), 1) <= 0), 0)

  logger.debug("Generated %d angles", len(angles))

  return angles


def cropToTumorMask(imageNode, maskNode, label=1, boundingBox=None):
  """
  Create a sitkImage of the segmented region of the image based on the input label.

  Create a sitkImage of the labelled region of the image, cropped to have a
  cuboid shape equal to the ijk boundaries of the label.

  Returns both the cropped version of the image and the cropped version of the labelmap, as well
  as the computed bounding box. The bounding box is returned as a tuple of indices: (L_x, U_x, L_y, U_y, L_z, U_z),
  where 'L' and 'U' are lower and upper bound, respectively, and 'x', 'y' and 'z' the three image dimensions.

  This can be used in subsequent calls to this function for the same images. This
  improves computation time, as it will reduce the number of calls to SimpleITK.LabelStatisticsImageFilter().

  :param label: [1], value of the label, onto which the image and mask must be cropped.
  :param boundingBox: [None], during a subsequent call, the boundingBox of a previous call can be passed
    here, removing the need to recompute it. During a first call to this function for a image/mask with a
    certain label, this value must be None or omitted.
  :return: Cropped image and mask (SimpleITK image instances) and the bounding box generated by SimpleITK
    LabelStatisticsImageFilter.

  """
  global logger

  oldMaskID = maskNode.GetPixelID()
  maskNode = sitk.Cast(maskNode, sitk.sitkInt32)
  size = numpy.array(maskNode.GetSize())

  # If the boundingbox has not yet been calculated, calculate it now and return it at the end of the function
  if boundingBox is None:
    logger.debug("Calculating bounding box")
    # Determine bounds
    lsif = sitk.LabelStatisticsImageFilter()
    lsif.Execute(imageNode, maskNode)
    boundingBox = numpy.array(lsif.GetBoundingBox(label))

  ijkMinBounds = boundingBox[0::2]
  ijkMaxBounds = size - boundingBox[1::2] - 1

  # Crop Image
  logger.debug('Cropping to size %s', (boundingBox[1::2] - boundingBox[0::2]) + 1)
  cif = sitk.CropImageFilter()
  try:
    cif.SetLowerBoundaryCropSize(ijkMinBounds)
    cif.SetUpperBoundaryCropSize(ijkMaxBounds)
  except TypeError:
    # newer versions of SITK/python want a tuple or list
    cif.SetLowerBoundaryCropSize(ijkMinBounds.tolist())
    cif.SetUpperBoundaryCropSize(ijkMaxBounds.tolist())
  croppedImageNode = cif.Execute(imageNode)
  croppedMaskNode = cif.Execute(maskNode)

  croppedMaskNode = sitk.Cast(croppedMaskNode, oldMaskID)

  return croppedImageNode, croppedMaskNode, boundingBox


def resampleImage(imageNode, maskNode, resampledPixelSpacing, interpolator=sitk.sitkBSpline, label=1, padDistance=5):
  """
  Resamples image or label to the specified pixel spacing (The default interpolator is Bspline)

  'imageNode' is a SimpleITK Object, and 'resampledPixelSpacing' is the output pixel spacing (list of 3 elements).

  Only part of the image and labelmap are resampled. The resampling grid is aligned to the input origin, but only voxels
  covering the area of the image defined by the bounding box and the padDistance are resampled. This results in a
  resampled and partially cropped return image and labelmap. Additional padding is required as some filters also sample
  voxels outside of segmentation boundaries. For feature calculation, image and mask are cropped to the bounding box
  without any additional padding, as the feature classes do not need the gray level values outside the segmentation.
  """
  global logger

  if imageNode is None or maskNode is None:
    return None

  oldSpacing = numpy.array(imageNode.GetSpacing())

  # If current spacing is equal to resampledPixelSpacing, no interpolation is needed
  if numpy.array_equal(oldSpacing, resampledPixelSpacing):
    logger.debug("New spacing equal to old, no resampling required")
    return imageNode, maskNode

  # Determine bounds of cropped volume in terms of original Index coordinate space
  lssif = sitk.LabelShapeStatisticsImageFilter()
  lssif.Execute(maskNode)
  bb = numpy.array(
    lssif.GetBoundingBox(label))  # LBound and size of the bounding box, as (L_X, L_Y, L_Z, S_X, S_Y, S_Z)

  # Do not resample in those directions where labelmap spans only one slice.
  oldSize = numpy.array(imageNode.GetSize())
  resampledPixelSpacing = numpy.where(bb[3:] != 1, resampledPixelSpacing, oldSpacing)

  spacingRatio = oldSpacing / resampledPixelSpacing

  # Determine bounds of cropped volume in terms of new Index coordinate space,
  # round down for lowerbound and up for upperbound to ensure entire segmentation is captured (prevent data loss)
  # Pad with an extra .5 to prevent data loss in case of upsampling. For Ubound this is (-1 + 0.5 = -0.5)
  bbNewLBound = numpy.floor((bb[:3] - 0.5) * spacingRatio - padDistance)
  bbNewUBound = numpy.ceil((bb[:3] + bb[3:] - 0.5) * spacingRatio + padDistance)

  # Ensure resampling is not performed outside bounds of original image
  maxUbound = numpy.ceil(oldSize * spacingRatio) - 1
  bbNewLBound = numpy.where(bbNewLBound < 0, 0, bbNewLBound)
  bbNewUBound = numpy.where(bbNewUBound > maxUbound, maxUbound, bbNewUBound)

  # Calculate the new size. Cast to int to prevent error in sitk.
  newSize = numpy.array(bbNewUBound - bbNewLBound + 1, dtype='int').tolist()

  # Determine continuous index of bbNewLBound in terms of the original Index coordinate space
  bbOriginalLBound = bbNewLBound / spacingRatio

  # Origin is located in center of first voxel, e.g. 1/2 of the spacing
  # from Corner, which corresponds to 0 in the original Index coordinate space.
  # The new spacing will be in 0 the new Index coordinate space. Here we use continuous
  # index to calculate where the new 0 of the new Index coordinate space (of the original volume
  # in terms of the original spacing, and add the minimum bounds of the cropped area to
  # get the new Index coordinate space of the cropped volume in terms of the original Index coordinate space.
  # Then use the ITK functionality to bring the contiuous index into the physical space (mm)
  newOriginIndex = numpy.array(.5 * (resampledPixelSpacing - oldSpacing) / oldSpacing)
  newCroppedOriginIndex = newOriginIndex + bbOriginalLBound
  newOrigin = imageNode.TransformContinuousIndexToPhysicalPoint(newCroppedOriginIndex)

  oldImagePixelType = imageNode.GetPixelID()
  oldMaskPixelType = maskNode.GetPixelID()

  imageDirection = numpy.array(imageNode.GetDirection())

  logger.info('Applying resampling from spacing %s and size %s to spacing %s and size %s',
              oldSpacing, oldSize, resampledPixelSpacing, newSize)

  try:
    if isinstance(interpolator, six.string_types):
      interpolator = getattr(sitk, interpolator)
  except:
    logger.warning('interpolator "%s" not recognized, using sitkBSpline', interpolator)
    interpolator = sitk.sitkBSpline

  rif = sitk.ResampleImageFilter()

  rif.SetOutputSpacing(resampledPixelSpacing)
  rif.SetOutputDirection(imageDirection)
  rif.SetSize(newSize)
  rif.SetOutputOrigin(newOrigin)

  logger.debug('Resampling image')
  rif.SetOutputPixelType(oldImagePixelType)
  rif.SetInterpolator(interpolator)
  resampledImageNode = rif.Execute(imageNode)

  logger.debug('Resampling mask')
  rif.SetOutputPixelType(oldMaskPixelType)
  rif.SetInterpolator(sitk.sitkNearestNeighbor)
  resampledMaskNode = rif.Execute(maskNode)

  return resampledImageNode, resampledMaskNode


def normalizeImage(image, scale=1, outliers=None):
  r"""
  Normalizes the image by centering it at the mean with standard deviation. Normalization is based on all gray values in
  the image, not just those inside the segementation.

  :math:`f(x) = \frac{s(x - \mu_x)}{\sigma_x}`

  Where:

  - :math:`x` and :math:`f(x)` are the original and normalized intensity, respectively.
  - :math:`\mu_x` and :math:`\sigma_x` are the mean and standard deviation of the image instensity values.
  - :math:`s` is an optional scaling defined by ``scale``. By default, it is set to 1.

  Optionally, outliers can be removed, in which case values for which :math:`x > \mu_x + n\sigma_x` or
  :math:`x < \mu_x - n\sigma_x` are set to :math:`\mu_x + n\sigma_x` and :math:`\mu_x - n\sigma_x`, respectively.
  Here, :math:`n>0` and defined by ``outliers``. This, in turn, is controlled by the ``removeOutliers`` parameter.
  Removal of outliers is done after the values of the image are normalized, but before ``scale`` is applied.
  """
  global logger
  logger.debug("Normalizing image with scale %d", scale)
  image = sitk.Normalize(image)

  if outliers is not None:
    logger.debug("Removing outliers > %g standard deviations", outliers)
    imageArr = sitk.GetArrayFromImage(image)

    imageArr[imageArr > outliers] = outliers
    imageArr[imageArr < -outliers] = -outliers

    newImage = sitk.GetImageFromArray(imageArr)
    newImage.CopyInformation(image)

  image *= scale

  return image


def applyThreshold(inputImage, lowerThreshold, upperThreshold, insideValue=None, outsideValue=0):
  # this mode is useful to generate the mask of thresholded voxels
  if insideValue:
    tif = sitk.BinaryThresholdImageFilter()
    tif.SetInsideValue(insideValue)
    tif.SetLowerThreshold(lowerThreshold)
    tif.SetUpperThreshold(upperThreshold)
  else:
    tif = sitk.ThresholdImageFilter()
    tif.SetLower(lowerThreshold)
    tif.SetUpper(upperThreshold)
  tif.SetOutsideValue(outsideValue)
  return tif.Execute(inputImage)


def getOriginalImage(inputImage, **kwargs):
  """
  This function does not apply any filter, but returns the original image. This function is needed to
  dyanmically expose the original image as a valid input image.

  :return: Yields original image, 'original' and ``kwargs``
  """
  global logger
  logger.debug("Yielding original image")
  yield inputImage, "original", kwargs


def getLoGImage(inputImage, **kwargs):
  """
  Apply Laplacian of Gaussian filter to input image and compute signature for each filtered image.

  Following settings are possible:

  - sigma: List of floats or integers, must be greater than 0. Sigma values to
    use for the filter (determines coarseness).

  N.B. Setting for sigma must be provided. If omitted, no LoG image features are calculated and the function
  will return an empty dictionary.

  Returned filter name reflects LoG settings:
  log-sigma-<sigmaValue>-3D.

  :return: Yields log filtered image for each specified sigma, corresponding filter name and ``kwargs``
  """
  global logger

  logger.debug("Generating LoG images")

  # Check if size of image is > 4 in all 3D directions (otherwise, LoG filter will fail)
  size = numpy.array(inputImage.GetSize())
  spacing = numpy.array(inputImage.GetSpacing())

  if numpy.min(size) < 4:
    logger.warning('Image too small to apply LoG filter, size: %s', size)
    return

  sigmaValues = kwargs.get('sigma', numpy.arange(5., 0., -.5))

  for sigma in sigmaValues:
    logger.info('Computing LoG with sigma %g', sigma)

    if sigma > 0.0:
      if numpy.all(size >= numpy.ceil(sigma / spacing) + 1):
        lrgif = sitk.LaplacianRecursiveGaussianImageFilter()
        lrgif.SetNormalizeAcrossScale(True)
        lrgif.SetSigma(sigma)
        inputImageName = "log-sigma-%s-mm-3D" % (str(sigma).replace('.', '-'))
        logger.debug('Yielding %s image', inputImageName)
        yield lrgif.Execute(inputImage), inputImageName, kwargs
      else:
        logger.warning('applyLoG: sigma(%g)/spacing(%s) + 1 must be greater than the size(%s) of the inputImage',
                       sigma,
                       spacing,
                       size)
    else:
      logger.warning('applyLoG: sigma must be greater than 0.0: %g', sigma)


def getWaveletImage(inputImage, **kwargs):
  """
  Apply wavelet filter to image and compute signature for each filtered image.

  Following settings are possible:

  - start_level [0]: integer, 0 based level of wavelet which should be used as first set of decompositions
    from which a signature is calculated
  - level [1]: integer, number of levels of wavelet decompositions from which a signature is calculated.
  - wavelet ["coif1"]: string, type of wavelet decomposition. Enumerated value, validated against possible values
    present in the ``pyWavelet.wavelist()``. Current possible values (pywavelet version 0.4.0) (where an
    aditional number is needed, range of values is indicated in []):

    - haar
    - dmey
    - sym[2-20]
    - db[1-20]
    - coif[1-5]
    - bior[1.1, 1.3, 1.5, 2.2, 2.4, 2.6, 2.8, 3.1, 3.3, 3.5, 3.7, 3.9, 4.4, 5.5, 6.8]
    - rbio[1.1, 1.3, 1.5, 2.2, 2.4, 2.6, 2.8, 3.1, 3.3, 3.5, 3.7, 3.9, 4.4, 5.5, 6.8]

  Returned filter name reflects wavelet type:
  wavelet[level]-<decompositionName>

  N.B. only levels greater than the first level are entered into the name.

  :return: Yields each wavelet decomposition and final approximation, corresponding filter name and ``kwargs``
  """
  global logger

  logger.debug("Generating Wavelet images")

  approx, ret = _swt3(inputImage, kwargs.get('wavelet', 'coif1'), kwargs.get('level', 1), kwargs.get('start_level', 0))

  for idx, wl in enumerate(ret, start=1):
    for decompositionName, decompositionImage in wl.items():
      logger.info('Computing Wavelet %s', decompositionName)

      if idx == 1:
        inputImageName = 'wavelet-%s' % (decompositionName)
      else:
        inputImageName = 'wavelet%s-%s' % (idx, decompositionName)
      logger.debug('Yielding %s image', inputImageName)
      yield decompositionImage, inputImageName, kwargs

  if len(ret) == 1:
    inputImageName = 'wavelet-LLL'
  else:
    inputImageName = 'wavelet%s-LLL' % (len(ret))
  logger.debug('Yielding approximation (%s) image', inputImageName)
  yield approx, inputImageName, kwargs


def _swt3(inputImage, wavelet="coif1", level=1, start_level=0):
  matrix = sitk.GetArrayFromImage(inputImage)
  matrix = numpy.asarray(matrix)
  if matrix.ndim != 3:
    raise ValueError("Expected 3D data array")

  original_shape = matrix.shape
  adjusted_shape = tuple([dim + 1 if dim % 2 != 0 else dim for dim in original_shape])
  data = matrix.copy()
  data.resize(adjusted_shape, refcheck=False)

  if not isinstance(wavelet, pywt.Wavelet):
    wavelet = pywt.Wavelet(wavelet)

  for i in range(0, start_level):
    H, L = _decompose_i(data, wavelet)
    LH, LL = _decompose_j(L, wavelet)
    LLH, LLL = _decompose_k(LL, wavelet)

    data = LLL.copy()

  ret = []
  for i in range(start_level, start_level + level):
    H, L = _decompose_i(data, wavelet)

    HH, HL = _decompose_j(H, wavelet)
    LH, LL = _decompose_j(L, wavelet)

    HHH, HHL = _decompose_k(HH, wavelet)
    HLH, HLL = _decompose_k(HL, wavelet)
    LHH, LHL = _decompose_k(LH, wavelet)
    LLH, LLL = _decompose_k(LL, wavelet)

    data = LLL.copy()

    dec = {'HHH': HHH,
           'HHL': HHL,
           'HLH': HLH,
           'HLL': HLL,
           'LHH': LHH,
           'LHL': LHL,
           'LLH': LLH}
    for decName, decImage in six.iteritems(dec):
      decTemp = decImage.copy()
      decTemp = numpy.resize(decTemp, original_shape)
      sitkImage = sitk.GetImageFromArray(decTemp)
      sitkImage.CopyInformation(inputImage)
      dec[decName] = sitkImage

    ret.append(dec)

  data = numpy.resize(data, original_shape)
  approximation = sitk.GetImageFromArray(data)
  approximation.CopyInformation(inputImage)

  return approximation, ret


def _decompose_i(data, wavelet):
  # process in i:
  H, L = [], []
  i_arrays = chain.from_iterable(data)
  for i_array in i_arrays:
    cA, cD = pywt.swt(i_array, wavelet, level=1, start_level=0)[0]
    H.append(cD)
    L.append(cA)
  H = numpy.hstack(H).reshape(data.shape)
  L = numpy.hstack(L).reshape(data.shape)
  return H, L


def _decompose_j(data, wavelet):
  # process in j:
  s = data.shape
  H, L = [], []
  j_arrays = chain.from_iterable(numpy.transpose(data, (0, 2, 1)))
  for j_array in j_arrays:
    cA, cD = pywt.swt(j_array, wavelet, level=1, start_level=0)[0]
    H.append(cD)
    L.append(cA)
  H = numpy.hstack(H).reshape((s[0], s[2], s[1])).transpose((0, 2, 1))
  L = numpy.hstack(L).reshape((s[0], s[2], s[1])).transpose((0, 2, 1))
  return H, L


def _decompose_k(data, wavelet):
  # process in k:
  H, L = [], []
  k_arrays = chain.from_iterable(numpy.transpose(data, (2, 1, 0)))
  for k_array in k_arrays:
    cA, cD = pywt.swt(k_array, wavelet, level=1, start_level=0)[0]
    H.append(cD)
    L.append(cA)
  H = numpy.asarray([slice for slice in numpy.split(numpy.vstack(H), data.shape[2])]).T
  L = numpy.asarray([slice for slice in numpy.split(numpy.vstack(L), data.shape[2])]).T
  return H, L


def getSquareImage(inputImage, **kwargs):
  r"""
  Computes the square of the image intensities.

  Resulting values are rescaled on the range of the initial original image and negative intensities are made
  negative in resultant filtered image.

  :math:`f(x) = (cx)^2,\text{ where } c=\displaystyle\frac{1}{\sqrt{\max(x)}}`

  Where :math:`x` and :math:`f(x)` are the original and filtered intensity, respectively.

  :return: Yields square filtered image, 'square' and ``kwargs``
  """
  global logger

  im = sitk.GetArrayFromImage(inputImage)
  im = im.astype('float64')
  coeff = 1 / numpy.sqrt(numpy.max(im))
  im = (coeff * im) ** 2
  im = sitk.GetImageFromArray(im)
  im.CopyInformation(inputImage)

  logger.debug('Yielding square image')
  yield im, "square", kwargs


def getSquareRootImage(inputImage, **kwargs):
  r"""
  Computes the square root of the absolute value of image intensities.

  Resulting values are rescaled on the range of the initial original image and negative intensities are made
  negative in resultant filtered image.

  :math:`f(x) = \left\{ {\begin{array}{lcl}
  \sqrt{cx} & \mbox{for} & x \ge 0 \\
  -\sqrt{-cx} & \mbox{for} & x < 0\end{array}} \right.,\text{ where } c=\max(x)`

  Where :math:`x` and :math:`f(x)` are the original and filtered intensity, respectively.

  :return: Yields square root filtered image, 'squareroot' and ``kwargs``
  """
  global logger

  im = sitk.GetArrayFromImage(inputImage)
  im = im.astype('float64')
  coeff = numpy.max(im)
  im[im > 0] = numpy.sqrt(im[im > 0] * coeff)
  im[im < 0] = - numpy.sqrt(-im[im < 0] * coeff)
  im = sitk.GetImageFromArray(im)
  im.CopyInformation(inputImage)

  logger.debug('Yielding squareroot image')
  yield im, "squareroot", kwargs


def getLogarithmImage(inputImage, **kwargs):
  r"""
  Computes the logarithm of the absolute value of the original image + 1.

  Resulting values are rescaled on the range of the initial original image and negative intensities are made
  negative in resultant filtered image.

  :math:`f(x) = \left\{ {\begin{array}{lcl}
  c\log{(x + 1)} & \mbox{for} & x \ge 0 \\
  -c\log{(-x + 1)} & \mbox{for} & x < 0\end{array}} \right. \text{, where } c=\left\{ {\begin{array}{lcl}
  \frac{\max(x)}{\log(\max(x) + 1)} & if & \max(x) \geq 0 \\
  \frac{\max(x)}{-\log(-\max(x) - 1)} & if & \max(x) < 0 \end{array}} \right.`

  Where :math:`x` and :math:`f(x)` are the original and filtered intensity, respectively.

  :return: Yields logarithm filtered image, 'logarithm' and ``kwargs``
  """
  global logger

  im = sitk.GetArrayFromImage(inputImage)
  im = im.astype('float64')
  im_max = numpy.max(im)
  im[im > 0] = numpy.log(im[im > 0] + 1)
  im[im < 0] = - numpy.log(- (im[im < 0] - 1))
  im = im * (im_max / numpy.max(im))
  im = sitk.GetImageFromArray(im)
  im.CopyInformation(inputImage)

  logger.debug('Yielding logarithm image')
  yield im, "logarithm", kwargs


def getExponentialImage(inputImage, **kwargs):
  r"""
  Computes the exponential of the original image.

  Resulting values are rescaled on the range of the initial original image.

  :math:`f(x) = e^{cx},\text{ where } c=\displaystyle\frac{\log(\max(x))}{\max(x)}`

  Where :math:`x` and :math:`f(x)` are the original and filtered intensity, respectively.

  :return: Yields exponential filtered image, 'exponential' and ``kwargs``
  """
  global logger

  im = sitk.GetArrayFromImage(inputImage)
  im = im.astype('float64')
  coeff = numpy.log(numpy.max(im)) / numpy.max(im)
  im = numpy.exp(coeff * im)
  im = sitk.GetImageFromArray(im)
  im.CopyInformation(inputImage)

  logger.debug('Yielding exponential image')
  yield im, "exponential", kwargs
