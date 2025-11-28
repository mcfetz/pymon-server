from flask import jsonify
from core import app, logger, SessionLocal
from db_models import Alarm


@app.route("/alarms/<int:alarmid>/ack", methods=["GET"])
def acknowledge_alarm(alarmid: int):
    """
    Acknowledge an alarm.
    ---
    tags:
      - alarms
    parameters:
      - in: path
        name: alarmid
        required: true
        schema:
          type: integer
        description: Alarm id
    responses:
      200:
        description: Alarm acknowledged
        content:
          application/json:
            schema:
              type: object
              properties:
                status:
                  type: string
                alarmid:
                  type: integer
      404:
        description: Alarm not found
      500:
        description: Internal error while acknowledging alarm
    """
    session = SessionLocal()
    try:
        alarm = session.get(Alarm, alarmid)
        if alarm is None:
            session.close()
            return jsonify({"error": f"Alarm with id {alarmid} not found"}), 404

        alarm.acknowledged = True
        session.commit()
        session.close()
        return jsonify({"status": "acknowledged", "alarmid": alarmid}), 200
    except Exception as e:
        session.rollback()
        session.close()
        logger.error("Error while acknowledging alarm %s: %s", alarmid, e)
        return jsonify({"error": "internal error"}), 500
