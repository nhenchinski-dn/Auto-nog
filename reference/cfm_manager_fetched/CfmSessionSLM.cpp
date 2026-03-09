#include "CfmSessionSLM.hpp"

#include <spdlog/fmt/fmt.h>
#include <system_events/include/dn_events_system.h>

#include "CfmLocalMep.hpp"
#include "CfmManager.hpp"
#include "CfmPackets.hpp"
#include "CfmTypes.hpp"
#include "corm_api.h"

using std::lock_guard;

static uint32_t g_test_id_gen = 1;

namespace cfm {

CfmSessionSLM::CfmSessionSLM(uint64_t sess_id,
                             stw_t* m_timer_wheel,
                             const SessionStartRequest& req,
                             const LMepOper& lmep_op)
    : CfmSession(sess_id, m_timer_wheel, req, lmep_op), test_id(g_test_id_gen++)
{
    if (0 == g_test_id_gen) g_test_id_gen = 1;
}

void CfmSessionSLM::UpdateLossValues()
{
    local_stats.loss_far_end = (local_stats.remote_tx > local_stats.my_tx)
                                   ? 0
                                   : local_stats.my_tx - local_stats.remote_tx;

    local_stats.loss_near_end = (local_stats.my_rx > local_stats.remote_tx)
                                    ? 0
                                    : local_stats.remote_tx - local_stats.my_rx;

    local_stats.loss_far_end_p =
        local_stats.my_tx ? local_stats.loss_far_end * 100.0 / local_stats.my_tx
                          : 0;

    local_stats.loss_near_end_p =
        local_stats.remote_tx
            ? local_stats.loss_near_end * 100.0 / local_stats.remote_tx
            : 0;
}

void CfmSessionSLM::SendPacket()
{
    try
    {
        SlmPacket packet(&cfg.src_mac,
                         &cfg.dst_mac,
                         cfg.inner_tag,
                         cfg.outer_tag,
                         cfg.pcp,
                         cfg.level,
                         cfg.mep_id,
                         test_id,
                         local_stats.my_tx,
                         cfg.inner_tpid,
                         cfg.outer_tpid);

        int ret = packet.Send(cfg.direction == MepDirection::UP, cfg.hw_id);
        if (!ret)
        {
            lock_guard _(m_lock);
            local_stats.my_tx++;
            UpdateLossValues();
            tx_done = true;
        }
    }
    catch (const std::exception& e)
    {
        CFM_LOG(DN_LOG_ERROR, "INITIATOR SLM send packet failed %s", e.what());
    }
}

void CfmSessionSLM::HandlePacket(wb_pkt* pkt)
{
    SlrPacket slr(pkt);
    lock_guard _(m_lock);

    min_remote_tx = std::min(min_remote_tx, slr.remote_tx);
    max_remote_tx = std::max(max_remote_tx, slr.remote_tx);

    rx_done = true;
    local_stats.my_rx++;
    local_stats.remote_tx = max_remote_tx - min_remote_tx + 1;
    local_stats.remote_rx = local_stats.remote_tx;
    local_stats.my_tx_slr = slr.my_tx;
    local_stats.remote_tx_slr = slr.remote_tx;

    UpdateLossValues();
}

static inline void send_proactive_event(
    const Proactive& pa,
    const LocalMepData& lmep,
    const CfmSessionStatsSLM& stats) noexcept
{
    const auto& thr = std::get<Proactive::SlmThreshold>(*pa.event_threshold);

    uint32_t thr_type = 0;
    uint32_t val = UINT32_MAX;
    std::string thresh_str;
    std::string val_str;

    const std::time_t tm = time(NULL);
    char tbuf[32];
    strftime(tbuf, sizeof(tbuf), "%F %T %z", std::gmtime(&tm));

    if (thr.far_end_loss && *thr.far_end_loss < stats.loss_far_end_p)
    {
        thr_type = 7;
        val = stats.loss_far_end_p * 100;
        thresh_str = fmt::format("{}%", *thr.far_end_loss);
        val_str = fmt::format("{}%", stats.loss_far_end_p);
    }
    else if (thr.near_end_loss && *thr.near_end_loss < stats.loss_near_end_p)
    {
        thr_type = 8;
        val = stats.loss_near_end_p * 100;
        thresh_str = fmt::format("{}%", *thr.near_end_loss);
        val_str = fmt::format("{}%", stats.loss_near_end_p);
    }

    if (val < UINT32_MAX)
        send_cfm_proactive_test_failure(2, // SLM
                                        pa.name.c_str(),
                                        pa.result_index,
                                        lmep.mep_id,
                                        lmep.ma_id.c_str(),
                                        lmep.md_id.c_str(),
                                        lmep.md_level,
                                        thresh_str.c_str(),
                                        val_str.c_str(),
                                        tbuf,
                                        thr_type,
                                        val);
}

void CfmSessionSLM::TimerCb(stw_tmr_t* ptimer, void* pdata)
{
    CfmSessionSLM* sess = static_cast<CfmSessionSLM*>(pdata);

    if (sess->state == SESS_STATE_RUNNING)
    {
        sess->SendPacket();
        if (sess->local_stats.my_tx == sess->cfg.pkt_count)
        {
            sess->state = SESS_STATE_WAIT_LAST_PACKET;
            stw_timer_start(sess->m_timer_wheel,
                            ptimer,
                            CFM_INITIATOR_WAIT_LAST_PACKET,
                            0,
                            CfmSessionSLM::TimerCb,
                            sess);
        }
    }
    else if (sess->state == SESS_STATE_WAIT_LAST_PACKET)
    {
        stw_timer_stop(sess->m_timer_wheel, ptimer);
        sess->state = SESS_STATE_DONE;

        const auto& cfg = sess->cfg;

        if (cfg.proactive && cfg.proactive->event_threshold)
        {
            const auto& lmeps =
                CfmManager::GetInstance().GetOperation().data.Meps;

            const auto lmep_it = lmeps.find(cfg.oam_id);
            if (lmeps.end() == lmep_it) return;

            const auto& lmep = lmep_it->second.config;

            const auto& pa = *cfg.proactive;
            send_proactive_event(pa, lmep, sess->local_stats);
        }
    }
}

static void proactiveResultInvalid(const Proactive&) noexcept;

SessionStartStatus CfmSessionSLM::Start()
{
    auto& oper = CfmManager::GetInstance().GetOperation();

    auto lmep_oper_it = oper.data.Meps.find(cfg.oam_id);
    if (lmep_oper_it == oper.data.Meps.end())
    {
        CFM_LOG(DN_LOG_ERR, "Initiator SLM: Failed to find MEP %d", cfg.oam_id);

        if (cfg.proactive) proactiveResultInvalid(*cfg.proactive);

        state = SESS_STATE_ABORT;
        return SESS_START_ERR_MISSING_MEP;
    }

    int rc = stw_timer_start(m_timer_wheel,
                             &m_timer,
                             0,
                             cfg.pkt_interval_ms,
                             CfmSessionSLM::TimerCb,
                             this);

    if (RC_STW_OK != rc)
    {
        CFM_LOG(DN_LOG_ERR, "INITIATOR SLM start failed timer %d", rc);
        state = SESS_STATE_DONE;

        return SESS_START_ERR;
    }

    state = SESS_STATE_RUNNING;

    return SESS_START_OK;
}

SessionStopStatus CfmSessionSLM::Stop()
{
    stw_timer_stop(m_timer_wheel, &m_timer);

    if (state != SESS_STATE_DONE)
    {
        cfg.proactive ? proactiveResultInvalid(*cfg.proactive)
                      : OperTestresultInvalid(SessionOpcode::SESS_SLM);
    }

    state = SESS_STATE_ABORT;

    return SESS_STOP_OK;
}

static void saveOperResult(uint64_t,
                           SessionState,
                           const CfmSessionStatsSLM&,
                           const LocalMepData&) noexcept;
static void updateProactiveResult(SessionState,
                                  const Proactive&,
                                  const CfmSessionStatsSLM&) noexcept;

void CfmSessionSLM::PushOperResults()
{
    const auto& lmeps = CfmManager::GetInstance().GetOperation().data.Meps;

    auto lmep_it = lmeps.find(cfg.oam_id);
    if (lmeps.end() == lmep_it) return;

    lock_guard _(m_lock);

    cfg.proactive
        ? updateProactiveResult(state, *cfg.proactive, local_stats)
        : saveOperResult(sess_id, state, local_stats, lmep_it->second.config);
}

bool CfmSessionSLM::OperTestInfoInitial(corm_obj* cobj) const
{
    // FIELD__count
    int rc = corm_u16_set(cobj, OD_FIELD_(SLM(Testinfo), count), cfg.pkt_count);
    // FIELD__interval
    rc |= corm_u16_set(
        cobj, OD_FIELD_(SLM(Testinfo), interval), cfg.pkt_interval_ms / 1000);

    return not rc;
}

void saveOperResult(uint64_t sess_id,
                    SessionState state,
                    const CfmSessionStatsSLM& stats,
                    const LocalMepData& lmep) noexcept
{
    const auto mep_id_str = fmt::format("{}", lmep.mep_id);
    std::array<const char*, 3> lmep_key = {
        lmep.md_id.c_str(),
        lmep.ma_id.c_str(),
        mep_id_str.c_str(),
    };

    CFM_LOG(DN_LOG_INFO,
            "SLM Testresults %s %lx session - MD %s MA %s LMEP %d",
            SessionState::SESS_STATE_DONE == state ? "'valid'" : "",
            sess_id,
            lmep.md_id.c_str(),
            lmep.ma_id.c_str(),
            lmep.mep_id);

    std::array<corm_obj*, 4> objs;

    auto obj_r = corm_obj_new(
        OD_CLASS_(SLM(Testresults)), lmep_key.data(), lmep_key.size());

    if (not obj_r)
    {
        CFM_LOG(DN_LOG_ERR,
                "Failed to allocate SLM Testinfo corm_obj for session id %lx",
                sess_id);

        return;
    }
    objs[0] = obj_r;
    int32_t n_objs = 1;

    int rc = corm_uint32_set( // FIELD__synthetic_loss_pdus_sent /*uint32_t*/
        obj_r,
        OD_FIELD_(SLM(Testresults), synthetic_loss_pdus_sent),
        stats.my_tx);
    rc |= corm_uint32_set( // FIELD__synthetic_loss_pdus_received /*uint32_t*/
        obj_r,
        OD_FIELD_(SLM(Testresults), synthetic_loss_pdus_received),
        stats.my_rx);
    rc |= corm_uint32_set( // FIELD__slm_received_by_remote_mep /*uint32_t*/
        obj_r,
        OD_FIELD_(SLM(Testresults), slm_received_by_remote_mep),
        stats.remote_tx);
    rc |= corm_uint32_set( // FIELD__missing_slr /*uint32_t*/
        obj_r,
        OD_FIELD_(SLM(Testresults), missing_slr),
        stats.my_tx - stats.my_rx);
    rc |= corm_uint32_set( // FIELD__local_txfcl_value /*uint32_t*/
        obj_r,
        OD_FIELD_(SLM(Testresults), local_txfcl_value),
        stats.my_tx ? stats.my_tx - 1 : 0);
    rc |= corm_uint32_set( // FIELD__local_rxfcl_value /*uint32_t*/
        obj_r,
        OD_FIELD_(SLM(Testresults), local_rxfcl_value),
        stats.my_rx);
    rc |= corm_uint32_set( // FIELD__last_slr_txfcf_tc /*uint32_t*/
        obj_r,
        OD_FIELD_(SLM(Testresults), last_slr_txfcf_tc),
        stats.my_tx_slr);
    rc |= corm_uint32_set( // FIELD__last_slr_txfcb_tc /*uint32_t*/
        obj_r,
        OD_FIELD_(SLM(Testresults), last_slr_txfcb_tc),
        stats.remote_tx_slr);

    if (SessionState::SESS_STATE_DONE == state)
    {
        rc |= corm_enum_set( // FIELD__measurement_validity
            obj_r,
            OD_FIELD_(SLM(Testresults), measurement_validity),
            OD_FIELD_(SLM(TestresultsMeasurementvalidityEnum), valid));

        auto obj_i = corm_obj_new(
            OD_CLASS_(SLM(Testinfo)), lmep_key.data(), lmep_key.size());
        if (obj_i)
        {
            objs[n_objs++] = obj_i;

            const std::time_t tm = time(NULL);
            char tbuf[32];
            // ISO 8601
            strftime(tbuf, sizeof(tbuf), "%F %T %z", std::gmtime(&tm));

            // FIELD__end_time
            rc |= corm_string_set(
                obj_i, OD_FIELD_(SLM(Testinfo), end_time), tbuf);
        }
    }

    auto obj_ne = corm_obj_new(OD_CLASS_(SLM(TestresultsFramelossNearend)),
                               lmep_key.data(),
                               lmep_key.size());
    if (obj_ne)
    {
        objs[n_objs++] = obj_ne;
        // FIELD__frame_loss_near_end_count /*uint32_t*/
        rc |= corm_uint32_set(obj_ne,
                              OD_FIELD_(SLM(TestresultsFramelossNearend),
                                        frame_loss_near_end_count),
                              stats.loss_near_end);
        // FIELD__frame_loss_near_end_percentage /*double64_t*/
        rc |= corm_double64_set(obj_ne,
                                OD_FIELD_(SLM(TestresultsFramelossNearend),
                                          frame_loss_near_end_percentage),
                                stats.loss_near_end_p);
    }

    auto obj_fe = corm_obj_new(OD_CLASS_(SLM(TestresultsFramelossFarend)),
                               lmep_key.data(),
                               lmep_key.size());
    if (obj_fe)
    {
        objs[n_objs++] = obj_fe;
        // FIELD__frame_loss_far_end_count /*uint32_t*/
        rc |= corm_uint32_set(obj_fe,
                              OD_FIELD_(SLM(TestresultsFramelossFarend),
                                        frame_loss_far_end_count),
                              stats.loss_far_end);
        // FIELD__frame_loss_far_end_percentage /*double64_t*/
        rc |= corm_double64_set(obj_fe,
                                OD_FIELD_(SLM(TestresultsFramelossFarend),
                                          frame_loss_far_end_percentage),
                                stats.loss_far_end_p);
    }

    if (rc)
    {
        CFM_LOG(DN_LOG_ERR,
                "Failed corm_set for session id %lx - %d objects",
                sess_id,
                n_objs);
        corm_obj_destroy_array(objs.data(), n_objs);
    }
    else if (auto sent = dbclient_set(
                 objs.data(), n_objs, E_DBCLIENT_FLAGS_FREE_NON_SENT_MSGS);
             n_objs != sent)
    {
        CFM_LOG(DN_LOG_ERR,
                "Failed dbclient_set for session id %lx - %d objects",
                sess_id,
                n_objs);
    }
}

void updateProactiveResult(SessionState state,
                           const Proactive& proa,
                           const CfmSessionStatsSLM& stats) noexcept
{
    const auto idx = fmt::format("{}", proa.result_index);
    std::array<const char*, 2> key = {
        proa.name.c_str(),
        idx.c_str(),
    };

    auto cid = PA_CLASS_(P_SLM(MeasurementresultItem));
    auto obj_r = corm_obj_new(cid, key.data(), key.size());

    if (not obj_r)
    {
        CFM_LOG(DN_LOG_ERR,
                "Failed to allocate SLM MeasurementresultItem corm_obj for "
                "session %s result index %d",
                proa.name.c_str(),
                proa.result_index);
        return;
    }

    std::array<corm_obj*, 3> objs;

    objs[0] = obj_r;
    int32_t n_objs = 1;

    int rc = corm_uint32_set( // FIELD__synthetic_loss_pdus_sent /*uint32_t*/
        obj_r,
        PA_FIELD_(P_SLM(MeasurementresultItem), synthetic_loss_pdus_sent),
        stats.my_tx);
    rc |= corm_uint32_set( // FIELD__synthetic_loss_pdus_received /*uint32_t*/
        obj_r,
        PA_FIELD_(P_SLM(MeasurementresultItem), synthetic_loss_pdus_received),
        stats.my_rx);
    rc |= corm_uint32_set( // FIELD__slm_received_by_remote_mep /*uint32_t*/
        obj_r,
        PA_FIELD_(P_SLM(MeasurementresultItem), slm_received_by_remote_mep),
        stats.remote_tx);
    rc |= corm_uint32_set( // FIELD__missing_slr /*uint32_t*/
        obj_r,
        PA_FIELD_(P_SLM(MeasurementresultItem), missing_slr),
        stats.my_tx - stats.my_rx);
    rc |= corm_uint32_set( // FIELD__local_txfc1_value /*uint32_t*/
        obj_r,
        PA_FIELD_(P_SLM(MeasurementresultItem), local_txfcl_value),
        stats.my_tx ? stats.my_tx - 1 : 0);
    rc |= corm_uint32_set( // FIELD__local_rxfc1_value /*uint32_t*/
        obj_r,
        PA_FIELD_(P_SLM(MeasurementresultItem), local_rxfcl_value),
        stats.my_rx);
    rc |= corm_uint32_set( // FIELD__last_slr_txfcf_tc /*uint32_t*/
        obj_r,
        PA_FIELD_(P_SLM(MeasurementresultItem), last_slr_txfcf_tc),
        stats.my_tx_slr);
    rc |= corm_uint32_set( // FIELD__last_slr_txfcb_tc /*uint32_t*/
        obj_r,
        PA_FIELD_(P_SLM(MeasurementresultItem), last_slr_txfcb_tc),
        stats.remote_tx_slr);

    if (SessionState::SESS_STATE_DONE == state)
    {
        rc |= corm_enum_set( // FIELD__measurement_validity
            obj_r,
            PA_FIELD_(P_SLM(MeasurementresultItem), measurement_validity),
            PA_FIELD_(P_SLM(MeasurementresultMeasurementvalidityEnum), valid));

        const std::time_t tm = time(NULL);
        char tbuf[32];
        // ISO 8601
        strftime(tbuf, sizeof(tbuf), "%F %T %z", std::gmtime(&tm));

        // FIELD__end_time
        rc |= corm_string_set(
            obj_r, PA_FIELD_(P_SLM(MeasurementresultItem), end_time), tbuf);
    }

    auto obj_ne =
        corm_obj_new(PA_CLASS_(P_SLM(MeasurementresultFramelossNearend)),
                     key.data(),
                     key.size());
    if (obj_ne)
    {
        objs[n_objs++] = obj_ne;
        // FIELD__frame_loss_near_end_count /*uint32_t*/
        rc |=
            corm_uint32_set(obj_ne,
                            PA_FIELD_(P_SLM(MeasurementresultFramelossNearend),
                                      frame_loss_near_end_count),
                            stats.loss_near_end);
        // FIELD__frame_loss_near_end_percentage /*double64_t*/
        rc |= corm_double64_set(
            obj_ne,
            PA_FIELD_(P_SLM(MeasurementresultFramelossNearend),
                      frame_loss_near_end_percentage),
            stats.loss_near_end_p);
    }

    auto obj_fe =
        corm_obj_new(PA_CLASS_(P_SLM(MeasurementresultFramelossFarend)),
                     key.data(),
                     key.size());

    if (obj_fe)
    {
        objs[n_objs++] = obj_fe;
        // FIELD__frame_loss_far_end_count /*uint32_t*/
        rc |= corm_uint32_set(obj_fe,
                              PA_FIELD_(P_SLM(MeasurementresultFramelossFarend),
                                        frame_loss_far_end_count),
                              stats.loss_far_end);
        // FIELD__frame_loss_far_end_percentage /*double64_t*/
        rc |=
            corm_double64_set(obj_fe,
                              PA_FIELD_(P_SLM(MeasurementresultFramelossFarend),
                                        frame_loss_far_end_percentage),
                              stats.loss_far_end_p);
    }

    if (rc)
    {
        CFM_LOG(DN_LOG_ERR,
                "Failed corm_set for session %s result index %d - %d objects",
                proa.name.c_str(),
                proa.result_index,
                n_objs);
        corm_obj_destroy_array(objs.data(), n_objs);
    }
    else if (auto sent = dbclient_set(
                 objs.data(), n_objs, E_DBCLIENT_FLAGS_FREE_NON_SENT_MSGS);
             n_objs != sent)
    {
        CFM_LOG(
            DN_LOG_ERR,
            "Failed dbclient_set for session %s result index %d - %d objects",
            proa.name.c_str(),
            proa.result_index,
            n_objs);
    }
}

void proactiveResultInvalid(const Proactive& proa) noexcept
{
    const auto idx = fmt::format("{}", proa.result_index);
    std::array<const char*, 2> key = {
        proa.name.c_str(),
        idx.c_str(),
    };

    auto obj = corm_obj_new(
        PA_CLASS_(P_SLM(MeasurementresultItem)), key.data(), key.size());

    if (not obj)
    {
        CFM_LOG(DN_LOG_ERR,
                "Failed to allocate SLM Measurementresult corm_obj for "
                "session %s result index %d",
                proa.name.c_str(),
                proa.result_index);
        return;
    }

    int rc = corm_enum_set( // FIELD__measurement_validity
        obj,
        PA_FIELD_(P_SLM(MeasurementresultItem), measurement_validity),
        PA_FIELD_(P_SLM(MeasurementresultMeasurementvalidityEnum), invalid));

    const std::time_t tm = time(NULL);
    char tbuf[32];
    // ISO 8601
    strftime(tbuf, sizeof(tbuf), "%F %T %z", std::gmtime(&tm));

    // FIELD__end_time
    rc |= corm_string_set(
        obj, PA_FIELD_(P_SLM(MeasurementresultItem), end_time), tbuf);

    if (rc)
    {
        CFM_LOG(DN_LOG_ERR,
                "Failed corm_set for session %s result index %d",
                proa.name.c_str(),
                proa.result_index);
        corm_obj_destroy(obj);
    }
    else if (auto sent =
                 dbclient_set(&obj, 1, E_DBCLIENT_FLAGS_FREE_NON_SENT_MSGS);
             not sent)
    {
        CFM_LOG(DN_LOG_ERR,
                "Failed dbclient_set for session %s result index %d",
                proa.name.c_str(),
                proa.result_index);
    }
}

} // namespace cfm
