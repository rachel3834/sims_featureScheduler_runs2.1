#!/usr/bin/env python

import numpy as np
import matplotlib.pylab as plt
import healpy as hp
from rubin_sim.scheduler.modelObservatory import Model_observatory
from rubin_sim.scheduler.schedulers import Core_scheduler, simple_filter_sched
from rubin_sim.scheduler.utils import (Sky_area_generator,
                                       make_rolling_footprints)
import rubin_sim.scheduler.basis_functions as bf
from rubin_sim.scheduler.surveys import (Greedy_survey, Blob_survey, Scripted_survey)
from rubin_sim.scheduler import sim_runner
import rubin_sim.scheduler.detailers as detailers
import sys
import subprocess
import os
import argparse
from make_ddf_survey import generate_ddf_scheduled_obs


def gen_greedy_surveys(nside=32, nexp=2, exptime=30., filters=['r', 'i', 'z', 'y'],
                       camera_rot_limits=[-80., 80.],
                       shadow_minutes=60., max_alt=76., moon_distance=30., ignore_obs='DD',
                       m5_weight=3., footprint_weight=0.75, slewtime_weight=3.,
                       stayfilter_weight=3., footprints=None):
    """
    Make a quick set of greedy surveys

    This is a convienence function to generate a list of survey objects that can be used with
    rubin_sim.scheduler.schedulers.Core_scheduler.
    To ensure we are robust against changes in the sims_featureScheduler codebase, all kwargs are
    explicitly set.

    Parameters
    ----------
    nside : int (32)
        The HEALpix nside to use
    nexp : int (1)
        The number of exposures to use in a visit.
    exptime : float (30.)
        The exposure time to use per visit (seconds)
    filters : list of str (['r', 'i', 'z', 'y'])
        Which filters to generate surveys for.
    camera_rot_limits : list of float ([-80., 80.])
        The limits to impose when rotationally dithering the camera (degrees).
    shadow_minutes : float (60.)
        Used to mask regions around zenith (minutes)
    max_alt : float (76.
        The maximium altitude to use when masking zenith (degrees)
    moon_distance : float (30.)
        The mask radius to apply around the moon (degrees)
    ignore_obs : str or list of str ('DD')
        Ignore observations by surveys that include the given substring(s).
    m5_weight : float (3.)
        The weight for the 5-sigma depth difference basis function
    footprint_weight : float (0.3)
        The weight on the survey footprint basis function.
    slewtime_weight : float (3.)
        The weight on the slewtime basis function
    stayfilter_weight : float (3.)
        The weight on basis function that tries to stay avoid filter changes.
    """
    # Define the extra parameters that are used in the greedy survey. I
    # think these are fairly set, so no need to promote to utility func kwargs
    greed_survey_params = {'block_size': 1, 'smoothing_kernel': None,
                           'seed': 42, 'camera': 'LSST', 'dither': True,
                           'survey_name': 'greedy'}

    surveys = []
    detailer = detailers.Camera_rot_detailer(min_rot=np.min(camera_rot_limits), max_rot=np.max(camera_rot_limits))

    for filtername in filters:
        bfs = []
        bfs.append((bf.M5_diff_basis_function(filtername=filtername, nside=nside), m5_weight))
        bfs.append((bf.Footprint_basis_function(filtername=filtername,
                                                footprint=footprints,
                                                out_of_bounds_val=np.nan, nside=nside), footprint_weight))
        bfs.append((bf.Slewtime_basis_function(filtername=filtername, nside=nside), slewtime_weight))
        bfs.append((bf.Strict_filter_basis_function(filtername=filtername), stayfilter_weight))
        # Masks, give these 0 weight
        bfs.append((bf.Zenith_shadow_mask_basis_function(nside=nside, shadow_minutes=shadow_minutes,
                                                         max_alt=max_alt), 0))
        bfs.append((bf.Moon_avoidance_basis_function(nside=nside, moon_distance=moon_distance), 0))

        bfs.append((bf.Filter_loaded_basis_function(filternames=filtername), 0))
        bfs.append((bf.Planet_mask_basis_function(nside=nside), 0))

        weights = [val[1] for val in bfs]
        basis_functions = [val[0] for val in bfs]
        surveys.append(Greedy_survey(basis_functions, weights, exptime=exptime, filtername=filtername,
                                     nside=nside, ignore_obs=ignore_obs, nexp=nexp,
                                     detailers=[detailer], **greed_survey_params))

    return surveys


def generate_blobs(nside, nexp=2, exptime=30., filter1s=['u', 'u', 'g', 'r', 'i', 'z', 'y'],
                   filter2s=['g', 'r', 'r', 'i', 'z', 'y', 'y'], pair_time=33.,
                   camera_rot_limits=[-80., 80.], n_obs_template=3,
                   season=300., season_start_hour=-4., season_end_hour=2.,
                   shadow_minutes=60., max_alt=76., moon_distance=30., ignore_obs='DD',
                   m5_weight=6., footprint_weight=1.5, slewtime_weight=3.,
                   stayfilter_weight=3., template_weight=12., footprints=None, u_nexp1=True,
                   scheduled_respect=45., good_seeing={'g': 3, 'r': 3, 'i': 3}, good_seeing_weight=3.,
                   mjd_start=1):
    """
    Generate surveys that take observations in blobs.

    Parameters
    ----------
    nside : int (32)
        The HEALpix nside to use
    nexp : int (1)
        The number of exposures to use in a visit.
    exptime : float (30.)
        The exposure time to use per visit (seconds)
    filter1s : list of str
        The filternames for the first set
    filter2s : list of str
        The filter names for the second in the pair (None if unpaired)
    pair_time : float (33)
        The ideal time between pairs (minutes)
    camera_rot_limits : list of float ([-80., 80.])
        The limits to impose when rotationally dithering the camera (degrees).
    n_obs_template : int (3)
        The number of observations to take every season in each filter
    season : float (300)
        The length of season (i.e., how long before templates expire) (days)
    season_start_hour : float (-4.)
        For weighting how strongly a template image needs to be observed (hours)
    sesason_end_hour : float (2.)
        For weighting how strongly a template image needs to be observed (hours)
    shadow_minutes : float (60.)
        Used to mask regions around zenith (minutes)
    max_alt : float (76.
        The maximium altitude to use when masking zenith (degrees)
    moon_distance : float (30.)
        The mask radius to apply around the moon (degrees)
    ignore_obs : str or list of str ('DD')
        Ignore observations by surveys that include the given substring(s).
    m5_weight : float (3.)
        The weight for the 5-sigma depth difference basis function
    footprint_weight : float (0.3)
        The weight on the survey footprint basis function.
    slewtime_weight : float (3.)
        The weight on the slewtime basis function
    stayfilter_weight : float (3.)
        The weight on basis function that tries to stay avoid filter changes.
    template_weight : float (12.)
        The weight to place on getting image templates every season
    u_nexp1 : bool (True)
        Add a detailer to make sure the number of expossures in a visit is always 1 for u observations.
    scheduled_respect : float (45)
        How much time to require there be before a pre-scheduled observation (minutes)
    """

    blob_survey_params = {'slew_approx': 7.5, 'filter_change_approx': 140.,
                          'read_approx': 2., 'min_pair_time': 15., 'search_radius': 30.,
                          'alt_max': 85., 'az_range': 90., 'flush_time': 30.,
                          'smoothing_kernel': None, 'nside': nside, 'seed': 42, 'dither': True,
                          'twilight_scale': True}

    surveys = []

    times_needed = [pair_time, pair_time*2]
    for filtername, filtername2 in zip(filter1s, filter2s):
        detailer_list = []
        detailer_list.append(detailers.Camera_rot_detailer(min_rot=np.min(camera_rot_limits),
                                                           max_rot=np.max(camera_rot_limits)))
        detailer_list.append(detailers.Close_alt_detailer())
        # List to hold tuples of (basis_function_object, weight)
        bfs = []

        if filtername2 is not None:
            bfs.append((bf.M5_diff_basis_function(filtername=filtername, nside=nside), m5_weight/2.))
            bfs.append((bf.M5_diff_basis_function(filtername=filtername2, nside=nside), m5_weight/2.))

        else:
            bfs.append((bf.M5_diff_basis_function(filtername=filtername, nside=nside), m5_weight))

        if filtername2 is not None:
            bfs.append((bf.Footprint_basis_function(filtername=filtername,
                                                    footprint=footprints,
                                                    out_of_bounds_val=np.nan, nside=nside), footprint_weight/2.))
            bfs.append((bf.Footprint_basis_function(filtername=filtername2,
                                                    footprint=footprints,
                                                    out_of_bounds_val=np.nan, nside=nside), footprint_weight/2.))
        else:
            bfs.append((bf.Footprint_basis_function(filtername=filtername,
                                                    footprint=footprints,
                                                    out_of_bounds_val=np.nan, nside=nside), footprint_weight))

        bfs.append((bf.Slewtime_basis_function(filtername=filtername, nside=nside), slewtime_weight))
        bfs.append((bf.Strict_filter_basis_function(filtername=filtername), stayfilter_weight))

        if filtername2 is not None:
            bfs.append((bf.N_obs_per_year_basis_function(filtername=filtername, nside=nside,
                                                         footprint=footprints.get_footprint(filtername),
                                                         n_obs=n_obs_template, season=season,
                                                         season_start_hour=season_start_hour,
                                                         season_end_hour=season_end_hour), template_weight/2.))
            bfs.append((bf.N_obs_per_year_basis_function(filtername=filtername2, nside=nside,
                                                         footprint=footprints.get_footprint(filtername2),
                                                         n_obs=n_obs_template, season=season,
                                                         season_start_hour=season_start_hour,
                                                         season_end_hour=season_end_hour), template_weight/2.))
        else:
            bfs.append((bf.N_obs_per_year_basis_function(filtername=filtername, nside=nside,
                                                         footprint=footprints.get_footprint(filtername),
                                                         n_obs=n_obs_template, season=season,
                                                         season_start_hour=season_start_hour,
                                                         season_end_hour=season_end_hour), template_weight))

        # Insert things for getting good seeing templates
        if filtername2 is not None:
            if filtername in list(good_seeing.keys()):
                bfs.append((bf.N_good_seeing_basis_function(filtername=filtername, nside=nside, mjd_start=mjd_start,
                                                            footprint=footprints.get_footprint(filtername),
                                                            n_obs_desired=good_seeing[filtername]), good_seeing_weight))
            if filtername2 in list(good_seeing.keys()):
                bfs.append((bf.N_good_seeing_basis_function(filtername=filtername2, nside=nside, mjd_start=mjd_start,
                                                            footprint=footprints.get_footprint(filtername2),
                                                            n_obs_desired=good_seeing[filtername2]), good_seeing_weight))
        else:
            if filtername in list(good_seeing.keys()):
                bfs.append((bf.N_good_seeing_basis_function(filtername=filtername, nside=nside, mjd_start=mjd_start,
                                                            footprint=footprints.get_footprint(filtername),
                                                            n_obs_desired=good_seeing[filtername]), good_seeing_weight))
        # Make sure we respect scheduled observations
        bfs.append((bf.Time_to_scheduled_basis_function(time_needed=scheduled_respect), 0))
        # Masks, give these 0 weight
        bfs.append((bf.Zenith_shadow_mask_basis_function(nside=nside, shadow_minutes=shadow_minutes, max_alt=max_alt,
                                                         penalty=np.nan, site='LSST'), 0.))
        bfs.append((bf.Moon_avoidance_basis_function(nside=nside, moon_distance=moon_distance), 0.))
        filternames = [fn for fn in [filtername, filtername2] if fn is not None]
        bfs.append((bf.Filter_loaded_basis_function(filternames=filternames), 0))
        if filtername2 is None:
            time_needed = times_needed[0]
        else:
            time_needed = times_needed[1]
        bfs.append((bf.Time_to_twilight_basis_function(time_needed=time_needed), 0.))
        bfs.append((bf.Not_twilight_basis_function(), 0.))
        bfs.append((bf.Planet_mask_basis_function(nside=nside), 0.))

        # unpack the basis functions and weights
        weights = [val[1] for val in bfs]
        basis_functions = [val[0] for val in bfs]
        if filtername2 is None:
            survey_name = 'blob, %s' % filtername
        else:
            survey_name = 'blob, %s%s' % (filtername, filtername2)
        if filtername2 is not None:
            detailer_list.append(detailers.Take_as_pairs_detailer(filtername=filtername2))

        if u_nexp1:
            detailer_list.append(detailers.Filter_nexp(filtername='u', nexp=1))
        surveys.append(Blob_survey(basis_functions, weights, filtername1=filtername, filtername2=filtername2,
                                   exptime=exptime,
                                   ideal_pair_time=pair_time,
                                   survey_note=survey_name, ignore_obs=ignore_obs,
                                   nexp=nexp, detailers=detailer_list, **blob_survey_params))

    return surveys


def generate_twi_blobs(nside, nexp=2, exptime=30., filter1s=['r', 'i', 'z', 'y'],
                       filter2s=['i', 'z', 'y', 'y'], pair_time=15.,
                       camera_rot_limits=[-80., 80.], n_obs_template=3,
                       season=300., season_start_hour=-4., season_end_hour=2.,
                       shadow_minutes=60., max_alt=76., moon_distance=30., ignore_obs='DD',
                       m5_weight=6., footprint_weight=1.5, slewtime_weight=3.,
                       stayfilter_weight=3., template_weight=12., footprints=None, repeat_night_weight=None,
                       wfd_footprint=None, scheduled_respect=15.):
    """
    Generate surveys that take observations in blobs.

    Parameters
    ----------
    nside : int (32)
        The HEALpix nside to use
    nexp : int (1)
        The number of exposures to use in a visit.
    exptime : float (30.)
        The exposure time to use per visit (seconds)
    filter1s : list of str
        The filternames for the first set
    filter2s : list of str
        The filter names for the second in the pair (None if unpaired)
    pair_time : float (22)
        The ideal time between pairs (minutes)
    camera_rot_limits : list of float ([-80., 80.])
        The limits to impose when rotationally dithering the camera (degrees).
    n_obs_template : int (3)
        The number of observations to take every season in each filter
    season : float (300)
        The length of season (i.e., how long before templates expire) (days)
    season_start_hour : float (-4.)
        For weighting how strongly a template image needs to be observed (hours)
    sesason_end_hour : float (2.)
        For weighting how strongly a template image needs to be observed (hours)
    shadow_minutes : float (60.)
        Used to mask regions around zenith (minutes)
    max_alt : float (76.
        The maximium altitude to use when masking zenith (degrees)
    moon_distance : float (30.)
        The mask radius to apply around the moon (degrees)
    ignore_obs : str or list of str ('DD')
        Ignore observations by surveys that include the given substring(s).
    m5_weight : float (3.)
        The weight for the 5-sigma depth difference basis function
    footprint_weight : float (0.3)
        The weight on the survey footprint basis function.
    slewtime_weight : float (3.)
        The weight on the slewtime basis function
    stayfilter_weight : float (3.)
        The weight on basis function that tries to stay avoid filter changes.
    template_weight : float (12.)
        The weight to place on getting image templates every season
    """

    blob_survey_params = {'slew_approx': 7.5, 'filter_change_approx': 140.,
                          'read_approx': 2., 'min_pair_time': 10., 'search_radius': 30.,
                          'alt_max': 85., 'az_range': 90., 'flush_time': 30.,
                          'smoothing_kernel': None, 'nside': nside, 'seed': 42, 'dither': True,
                          'twilight_scale': False, 'in_twilight': True}

    surveys = []

    times_needed = [pair_time, pair_time*2]
    for filtername, filtername2 in zip(filter1s, filter2s):
        detailer_list = []
        detailer_list.append(detailers.Camera_rot_detailer(min_rot=np.min(camera_rot_limits),
                                                           max_rot=np.max(camera_rot_limits)))
        detailer_list.append(detailers.Close_alt_detailer())
        # List to hold tuples of (basis_function_object, weight)
        bfs = []

        if filtername2 is not None:
            bfs.append((bf.M5_diff_basis_function(filtername=filtername, nside=nside), m5_weight/2.))
            bfs.append((bf.M5_diff_basis_function(filtername=filtername2, nside=nside), m5_weight/2.))

        else:
            bfs.append((bf.M5_diff_basis_function(filtername=filtername, nside=nside), m5_weight))

        if filtername2 is not None:
            bfs.append((bf.Footprint_basis_function(filtername=filtername,
                                                    footprint=footprints,
                                                    out_of_bounds_val=np.nan, nside=nside), footprint_weight/2.))
            bfs.append((bf.Footprint_basis_function(filtername=filtername2,
                                                    footprint=footprints,
                                                    out_of_bounds_val=np.nan, nside=nside), footprint_weight/2.))
        else:
            bfs.append((bf.Footprint_basis_function(filtername=filtername,
                                                    footprint=footprints,
                                                    out_of_bounds_val=np.nan, nside=nside), footprint_weight))

        bfs.append((bf.Slewtime_basis_function(filtername=filtername, nside=nside), slewtime_weight))
        bfs.append((bf.Strict_filter_basis_function(filtername=filtername), stayfilter_weight))

        if filtername2 is not None:
            bfs.append((bf.N_obs_per_year_basis_function(filtername=filtername, nside=nside,
                                                         footprint=footprints.get_footprint(filtername),
                                                         n_obs=n_obs_template, season=season,
                                                         season_start_hour=season_start_hour,
                                                         season_end_hour=season_end_hour), template_weight/2.))
            bfs.append((bf.N_obs_per_year_basis_function(filtername=filtername2, nside=nside,
                                                         footprint=footprints.get_footprint(filtername2),
                                                         n_obs=n_obs_template, season=season,
                                                         season_start_hour=season_start_hour,
                                                         season_end_hour=season_end_hour), template_weight/2.))
        else:
            bfs.append((bf.N_obs_per_year_basis_function(filtername=filtername, nside=nside,
                                                         footprint=footprints.get_footprint(filtername),
                                                         n_obs=n_obs_template, season=season,
                                                         season_start_hour=season_start_hour,
                                                         season_end_hour=season_end_hour), template_weight))
        if repeat_night_weight is not None:
            bfs.append((bf.Avoid_long_gaps_basis_function(nside=nside, filtername=None,
                                                          min_gap=0., max_gap=10./24., ha_limit=3.5,
                                                          footprint=wfd_footprint), repeat_night_weight))
        # Make sure we respect scheduled observations
        bfs.append((bf.Time_to_scheduled_basis_function(time_needed=scheduled_respect), 0))
        # Masks, give these 0 weight
        bfs.append((bf.Zenith_shadow_mask_basis_function(nside=nside, shadow_minutes=shadow_minutes, max_alt=max_alt,
                                                         penalty=np.nan, site='LSST'), 0.))
        bfs.append((bf.Moon_avoidance_basis_function(nside=nside, moon_distance=moon_distance), 0.))
        filternames = [fn for fn in [filtername, filtername2] if fn is not None]
        bfs.append((bf.Filter_loaded_basis_function(filternames=filternames), 0))
        if filtername2 is None:
            time_needed = times_needed[0]
        else:
            time_needed = times_needed[1]
        bfs.append((bf.Time_to_twilight_basis_function(time_needed=time_needed, alt_limit=12), 0.))
        bfs.append((bf.Planet_mask_basis_function(nside=nside), 0.))

        # unpack the basis functions and weights
        weights = [val[1] for val in bfs]
        basis_functions = [val[0] for val in bfs]
        if filtername2 is None:
            survey_name = 'blob_twi, %s' % filtername
        else:
            survey_name = 'blob_twi, %s%s' % (filtername, filtername2)
        if filtername2 is not None:
            detailer_list.append(detailers.Take_as_pairs_detailer(filtername=filtername2))
        surveys.append(Blob_survey(basis_functions, weights, filtername1=filtername, filtername2=filtername2,
                                   exptime=exptime,
                                   ideal_pair_time=pair_time,
                                   survey_note=survey_name, ignore_obs=ignore_obs,
                                   nexp=nexp, detailers=detailer_list, **blob_survey_params))

    return surveys


def ddf_surveys(detailers=None, ddf_file='ddf_1.npz', season_frac=0.1,
                low_season_frac=0.4, low_season_rate=0.3):
    obs_array = generate_ddf_scheduled_obs(season_frac=season_frac, low_season_frac=low_season_frac,
                                           low_season_rate=low_season_rate)
    #data = np.load(ddf_file)
    #obs_array = data['obs_array'].copy()
    survey = Scripted_survey([], detailers=detailers)
    survey.set_script(obs_array)
    return [survey]


def run_sched(surveys, survey_length=365.25, nside=32, fileroot='baseline_', verbose=False,
              extra_info=None, illum_limit=40.):
    years = np.round(survey_length/365.25)
    scheduler = Core_scheduler(surveys, nside=nside)
    n_visit_limit = None
    filter_sched = simple_filter_sched(illum_limit=illum_limit)
    observatory = Model_observatory(nside=nside)
    observatory, scheduler, observations = sim_runner(observatory, scheduler,
                                                      survey_length=survey_length,
                                                      filename=fileroot+'%iyrs.db' % years,
                                                      delete_past=True, n_visit_limit=n_visit_limit,
                                                      verbose=verbose, extra_info=extra_info,
                                                      filter_scheduler=filter_sched)


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", dest='verbose', action='store_true')
    parser.set_defaults(verbose=False)
    parser.add_argument("--survey_length", type=float, default=365.25*10)
    parser.add_argument("--outDir", type=str, default="")
    parser.add_argument("--maxDither", type=float, default=0.7, help="Dither size for DDFs (deg)")
    parser.add_argument("--moon_illum_limit", type=float, default=40., help="illumination limit to remove u-band")
    parser.add_argument("--nexp", type=int, default=2)
    parser.add_argument("--rolling_nslice", type=int, default=2)
    parser.add_argument("--rolling_strength", type=float, default=0.9)
    parser.add_argument("--dbroot", type=str)
    parser.add_argument("--gsw", type=float, default=3.0)
    parser.add_argument("--ddf_file", type=str, default='ddf.npz')
    parser.add_argument("--ddf_season_frac", type=float, default=0.1)
    parser.add_argument("--ddf_low_season_frac", type=float, default=0.4)
    parser.add_argument("--ddf_low_season_rate", type=float, default=0.3)

    args = parser.parse_args()
    survey_length = args.survey_length  # Days
    outDir = args.outDir
    verbose = args.verbose
    max_dither = args.maxDither
    illum_limit = args.moon_illum_limit
    nexp = args.nexp
    nslice = args.rolling_nslice
    scale = args.rolling_strength
    dbroot = args.dbroot
    gsw = args.gsw

    ddf_file = args.ddf_file
    ddf_season_frac = args.ddf_season_frac
    ddf_low_season_frac = args.ddf_low_season_frac
    ddf_low_season_rate = args.ddf_low_season_rate

    ddf_file = 'sf%.2f' % ddf_season_frac + ddf_file
    # Generate the DDF observations
    # obs_array = generate_ddf_scheduled_obs(season_frac=ddf_season_frac)
    # np.savez(ddf_file, obs_array=obs_array)

    nside = 32
    per_night = True  # Dither DDF per night

    camera_ddf_rot_limit = 75.

    extra_info = {}
    exec_command = ''
    for arg in sys.argv:
        exec_command += ' ' + arg
    extra_info['exec command'] = exec_command
    try:
        extra_info['git hash'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'])
    except subprocess.CalledProcessError:
        extra_info['git hash'] = 'Not in git repo'

    extra_info['file executed'] = os.path.realpath(__file__)

    # Use the filename of the script to name the output database
    if dbroot is None:
        fileroot = os.path.basename(sys.argv[0]).replace('.py', '') + '_'
    else:
        fileroot = dbroot + '_'
    file_end = 'sf%.2f_lsf%.1f_lsr%.1f_v2.1_' % (ddf_season_frac, ddf_low_season_frac, ddf_low_season_rate)

    sm = Sky_area_generator(nside=nside)

    footprints_hp_array, labels = sm.return_maps()
    wfd_indx = np.where((labels == 'lowdust') | (labels == 'LMC_SMC') | (labels == 'virgo'))[0]
    wfd_footprint = footprints_hp_array['r']*0
    wfd_footprint[wfd_indx] = 1

    footprints_hp = {}
    for key in footprints_hp_array.dtype.names:
        footprints_hp[key] = footprints_hp_array[key]

    repeat_night_weight = None

    observatory = Model_observatory(nside=nside)
    conditions = observatory.return_conditions()

    footprints = make_rolling_footprints(fp_hp=footprints_hp, mjd_start=conditions.mjd_start,
                                         sun_RA_start=conditions.sun_RA_start, nslice=nslice, scale=scale,
                                         nside=nside, wfd_indx=wfd_indx)

    # Set up the DDF surveys to dither
    u_detailer = detailers.Filter_nexp(filtername='u', nexp=1)
    dither_detailer = detailers.Dither_detailer(per_night=per_night, max_dither=max_dither)
    details = [detailers.Camera_rot_detailer(min_rot=-camera_ddf_rot_limit, max_rot=camera_ddf_rot_limit),
               dither_detailer, u_detailer]
    euclid_detailers = [detailers.Camera_rot_detailer(min_rot=-camera_ddf_rot_limit, max_rot=camera_ddf_rot_limit),
                        detailers.Euclid_dither_detailer(), u_detailer]
    ddfs = ddf_surveys(detailers=details, season_frac=ddf_season_frac, low_season_frac=ddf_low_season_frac,
                       low_season_rate=ddf_low_season_rate)

    greedy = gen_greedy_surveys(nside, nexp=nexp, footprints=footprints)

    blobs = generate_blobs(nside, nexp=nexp, footprints=footprints, mjd_start=conditions.mjd_start, good_seeing_weight=gsw)
    twi_blobs = generate_twi_blobs(nside, nexp=nexp,
                                   footprints=footprints,
                                   wfd_footprint=wfd_footprint,
                                   repeat_night_weight=repeat_night_weight)
    surveys = [ddfs, blobs, twi_blobs, greedy]
    run_sched(surveys, survey_length=survey_length, verbose=verbose,
              fileroot=os.path.join(outDir, fileroot+file_end), extra_info=extra_info,
              nside=nside, illum_limit=illum_limit)
