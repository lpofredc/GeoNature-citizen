#!/usr/bin/env python3
# -*- coding: utf-8 -*-


from datetime import datetime
import uuid
import json
import os
import uuid
from pprint import pprint

import requests
from flask import Blueprint, current_app, request
from flask_jwt_extended import jwt_optional
from geoalchemy2.shape import from_shape
from geojson import FeatureCollection
from shapely.geometry import Point, asShape
from werkzeug import FileStorage
from werkzeug.datastructures import ImmutableMultiDict

from gncitizen.core.commons.models import MediaModel
from gncitizen.core.ref_geo.models import LAreas
from gncitizen.core.taxonomy.models import Taxref
from gncitizen.core.users.models import UserModel

from gncitizen.utils.env import taxhub_lists_url, MEDIA_DIR
from gncitizen.utils.errors import GeonatureApiError
from gncitizen.utils.jwt import get_id_role_if_exists
from gncitizen.utils.media import allowed_file, save_upload_files
from gncitizen.utils.sqlalchemy import get_geojson_feature, json_resp
from gncitizen.utils.taxonomy import get_specie_from_cd_nom
from server import db

from .models import ObservationMediaModel, ObservationModel

routes = Blueprint("observations", __name__)

"""Used attributes in observation features"""
obs_keys = (
    "cd_nom",
    "id_observation",
    "obs_txt",
    "count",
    "date",
    "comment",
    "timestamp_create",
)


def generate_observation_geojson(id_observation):
    """generate observation in geojson format from observation id

      :param id_observation: Observation unique id
      :type id_observation: int

      :return features: Observations as a Feature dict
      :rtype features: dict
    """

    # Crée le dictionnaire de l'observation
    result = ObservationModel.query.get(id_observation)
    result_dict = result.as_dict(True)

    # Populate "geometry"
    features = []
    feature = get_geojson_feature(result.geom)

    # Populate "properties"
    for k in result_dict:
        if k in obs_keys:
            feature["properties"][k] = result_dict[k]

    # Get official taxref scientific and common names (first one) from cd_nom where cd_nom = cd_ref  # noqa: E501
    taxref = get_specie_from_cd_nom(feature["properties"]["cd_nom"])
    for k in taxref:
        feature["properties"][k] = taxref[k]
    features.append(feature)
    return features


def get_municipality_id_from_wkb_point(db, wkb):
    """Return municipality id from wkb geometry     

    :param db: db
    :param wkb: WKB geometry
    :type wkb: str

    :return: municipality id
    :rtype: int
    """
    try:
        query = (
            db.session.query(LAreas)
            .filter(LAreas.geom.ST_Intersects(wkb), LAreas.id_type == 101)
            .limit(1)
        )
        municipality_id = query[0]
        current_app.logger.debug(
            "[get_municipality_id_from_wkb_point] municipality id is {}".format(
                municipality_id
            )
        )
    except Exception as e:
        current_app.logger.debug(
            "[get_municipality_id_from_wkb_point] Can't get municipality id: {}".format(
                str(e)
            )
        )
    return municipality_id


@routes.route("/observations/<int:pk>")
@json_resp
def get_observation(pk):
    """Get on observation by id
         ---
         tags:
          - observations
         parameters:
          - name: pk
            in: path
            type: integer
            required: true
            example: 1
         definitions:
           cd_nom:
             type: integer
             description: cd_nom taxref
           geometry:
             type: dict
             description: Géométrie de la donnée
           name:
             type: string
           geom:
             type: geojson
         responses:
           200:
             description: A list of all observations
         """
    try:
        features = generate_observation_geojson(pk)
        return {"features": features}, 200
    except Exception as e:
        return {"error_message": str(e)}, 400


@routes.route("/observations", methods=["POST"])
@json_resp
@jwt_optional
def post_observation():
    """Post a observation
    add a observation to database
        ---
        tags:
          - observations
        # security:
        #   - bearerAuth: []
        summary: Creates a new observation (JWT auth optional, if used, obs_txt replaced by username)
        consumes:
          - multipart/form-data
        produces:
          - application/json
        parameters:
          - name: id_program
            required: true
            type: integer
            description: Program unique id
            example: 1
            default: 1
          - name: cd_nom
            required: true
            type: integer
            description: CD_Nom Taxref
            example: 3582
          - name: obs_txt
            required: false
            type: string
            default:  none
            description: User name
            example: Martin Dupont
          - name: count
            required: false
            type: integer
            description: Number of individuals
            default:  1
          - name: date
            required: true
            type: string
            description: Date
            required: false
            example: "2018-09-20"
          - name: geometry
            required: true
            type: string
            description: Geometry (GeoJson format)
            example: '{"type":"Point", "coordinates":[5,45]}'
        responses:
          200:
            description: Adding a observation
        """
    try:
        request_datas = request.form
        current_app.logger.debug(
            "[post_observation] request data:", request_datas
        )

        datas2db = {}
        for field in request_datas:
            if hasattr(ObservationModel, field):
                datas2db[field] = request_datas[field]
        current_app.logger.debug("[post_observation] datas2db: %s", datas2db)

        try:
            if request.files:

                current_app.logger.debug("request.files: %s", request.files)
                file = request.files.get("file", None)
                current_app.logger.debug(
                    "[post_observation] request.files: %s", request.files
                )

        except Exception as e:
            current_app.logger.debug("[post_observation] file ", e)
            raise GeonatureApiError(e)

        else:
            file = None

        try:
            newobs = ObservationModel(**datas2db)
        except Exception as e:
            current_app.logger.debug(e)
            raise GeonatureApiError(e)
        try:
            shape = asShape(json.loads(request_datas["geometry"]))
            newobs.geom = from_shape(Point(shape), srid=4326)
        except Exception as e:
            current_app.logger.debug(e)
            raise GeonatureApiError(e)

        id_role = get_id_role_if_exists()
        if id_role:
            newobs.id_role = id_role
            # for GDPR compatibility, we can't update obs_txt and email fields with user informations, user name will be be automaticaly must be added from user model
            # role = UserModel.query.get(id_role)
            # newobs.obs_txt = role.username
            # newobs.email = role.email
        else:
            if newobs.obs_txt is None or len(newobs.obs_txt) == 0:
                newobs.obs_txt = "Anonyme"

        newobs.uuid_sinp = uuid.uuid4()

        # Get municipality id
        newobs.municipality = get_municipality_id_from_wkb_point(
            db, newobs.geom
        )

        db.session.add(newobs)
        db.session.commit()
        # Réponse en retour
        features = generate_observation_geojson(newobs.id_observation)
        current_app.logger.debug("FEATURES: {}".format(features))
        # Enregistrement de la photo et correspondance Obs Photo
        try:
            file = save_upload_files(
                request.files,
                "obstax",
                datas2db["cd_nom"],
                newobs.id_observation,
                ObservationMediaModel,
            )
            current_app.logger.debug("ObsTax UPLOAD FILE {}".format(file))
            features[0]["properties"]["images"] = file

        except Exception as e:
            current_app.logger.debug("ObsTax ERROR ON FILE SAVING", str(e))
            raise GeonatureApiError(e)

        return (
            {"message": "New observation created", "features": features},
            200,
        )

    except Exception as e:
        current_app.logger.warning("[post_observation] Error: %s", str(e))
        return {"error_message": str(e)}, 400


@routes.route("/observations", methods=["GET"])
@json_resp
def get_observations():
    """Get all observations
        ---
        tags:
          - observations
        definitions:
          cd_nom:
            type: integer
            description: cd_nom taxref
          geometry:
            type: dict
            description: Géométrie de la donnée
          name:
            type: string
          geom:
            type: geometry
        responses:
          200:
            description: A list of all observations
        """
    try:
        observations = ObservationModel.query.all()
        features = []
        for observation in observations:
            feature = get_geojson_feature(observation.geom)
            observation_dict = observation.as_dict(True)
            for k in observation_dict:
                if k in obs_keys:
                    feature["properties"][k] = observation_dict[k]

            taxref = get_specie_from_cd_nom(feature["properties"]["cd_nom"])
            for k in taxref:
                feature["properties"][k] = taxref[k]
            features.append(feature)
        return FeatureCollection(features)
    except Exception as e:
        return {"error_message": str(e)}, 400


@routes.route("/observations/lists/<int:id>", methods=["GET"])
@json_resp
def get_observations_from_list(id):  # noqa: A002
    """Get all observations from a taxonomy list
    GET
        ---
        tags:
          - observations
        parameters:
          - name: id
            in: path
            type: integer
            required: true
            example: 1
        definitions:
          cd_nom:
            type: integer
            description: cd_nom taxref
          geometry:
            type: dict
            description: Géométrie de la donnée
          name:
            type: string
          geom:
            type: geometry
        responses:
          200:
            description: A list of all species lists
        """
    # taxhub_url = load_config()['TAXHUB_API_URL']
    taxhub_lists_taxa_url = taxhub_lists_url + "taxons/" + str(id)
    rtaxa = requests.get(taxhub_lists_taxa_url)
    if rtaxa.status_code == 200:
        try:
            taxa = rtaxa.json()["items"]
            current_app.logger.debug(taxa)
            features = []
            for t in taxa:
                current_app.logger.debug("R", t["cd_nom"])
                datas = ObservationModel.query.filter_by(
                    cd_nom=t["cd_nom"]
                ).all()
                for d in datas:
                    feature = get_geojson_feature(d.geom)
                    observation_dict = d.as_dict(True)
                    for k in observation_dict:
                        if k in obs_keys:
                            feature["properties"][k] = observation_dict[k]
                    taxref = get_specie_from_cd_nom(
                        feature["properties"]["cd_nom"]
                    )
                    for k in taxref:
                        feature["properties"][k] = taxref[k]
                    features.append(feature)
            return FeatureCollection(features)
        except Exception as e:
            return {"error_message": str(e)}, 400


@routes.route("programs/<int:id>/observations", methods=["GET"])
@json_resp
def get_program_observations(id):
    """Get all observations from a program
    GET
        ---
        tags:
          - observations
        parameters:
          - name: id
            in: path
            type: integer
            required: true
            example: 1
        definitions:
          cd_nom:
            type: integer
            description: cd_nom taxref
          geometry:
            type: dict
            description: Géométrie de la donnée
          name:
            type: string
          geom:
            type: geometry
        responses:
          200:
            description: A list of all species lists
        """
    try:
        observations = (
            db.session.query(
                ObservationModel,
                (LAreas.area_name + " (" + LAreas.area_code[:2] + ")").label(
                    "municipality"
                ),
            )
            .filter_by(id_program=id)
            .join(
                LAreas,
                LAreas.id_area == ObservationModel.municipality,
                isouter=True,
            )
            .all()
        )
        features = []
        for observation in observations:
            feature = get_geojson_feature(observation.ObservationModel.geom)
            feature["properties"]["municipality"] = observation.municipality
            observation_dict = observation.ObservationModel.as_dict(True)
            for k in observation_dict:
                if k in obs_keys:
                    feature["properties"][k] = observation_dict[k]
            taxref = get_specie_from_cd_nom(feature["properties"]["cd_nom"])
            for k in taxref:
                feature["properties"][k] = taxref[k]
            features.append(feature)
        current_app.logger.debug(FeatureCollection(features))
        return FeatureCollection(features)
    except Exception as e:
        return {"error_message": str(e)}, 400
