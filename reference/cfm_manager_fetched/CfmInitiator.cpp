#include "CfmInitiator.hpp"

#include <spdlog/fmt/fmt.h>

#include "CfmManager.hpp"
#include "CfmPackets.hpp"
#include "CfmSessionDMM.hpp"
#include "CfmSessionLBM.hpp"
#include "CfmSessionLTM.hpp"
#include "CfmSessionSLM.hpp"

namespace cfm {

using std::lock_guard;
using std::make_unique;

void CfmInitiator::SetTimerWheel(stw_t* timer_wheel)
{
    m_timer_wheel = timer_wheel;
    pro_sched.SetTimerWheel(timer_wheel);
}

void CfmInitiator::DeleteMepCB(uint32_t oam_id)
{
    SessionStopRequest req;

    CFM_LOG(DN_LOG_INFO, "INITIATOR Mep deleted %u", oam_id);
    for (int opcode = SESS_DMM; opcode < SESS_UNKNOWN; opcode++)
    {
        req.sess_id = sess_compute_id(oam_id, opcode);
        TerminateSession(req);
    }
}

static void OdTestInfoUpdate(const CfmSession&,
                             const SessionStartRequest&,
                             const LMepOper&) noexcept;
static void OdTestResultStatus(SessionOpcode,
                               const SessionStartResponse&,
                               const LMepOper&) noexcept;
static void ProactiveSessOperUpdate(const CfmSession&,
                                    const SessionStartRequest&,
                                    const LMepOper&) noexcept;
static void ProactiveSessResultUpdate(const SessionStartRequest&,
                                      bool) noexcept;

SessionStartResponse CfmInitiator::CreateSession(const SessionStartRequest& req)
{
    SessionStartResponse ret;

    ret.status = SESS_START_OK;
    ret.sess_id = sess_compute_id(req.oam_id, req.type);

    auto& mgr = CfmManager::GetInstance();

    if (mgr.GetOamLock().IsBusy())
    {
        ret.status = SESS_START_ERR_COMMIT_PROGRESS;
        if (req.proactive) ProactiveSessResultUpdate(req, false);
        return ret;
    }

    const auto& lmeps = mgr.GetOperation().data.Meps;

    // Is this thread-safe?
    auto lmep_it = lmeps.find(req.oam_id);
    if (lmeps.end() == lmep_it)
    {
        ret.status = SESS_START_ERR_MISSING_MEP;
        if (req.proactive) ProactiveSessResultUpdate(req, false);
        return ret;
    }

    const auto& lmep_oper = lmep_it->second;

    if (!lmep_oper.config.admin_state)
    {
        ret.status = SESS_START_ERR_DISABLED_MEP;
        if (req.proactive) ProactiveSessResultUpdate(req, false);
        return ret;
    }

    {
        lock_guard _(m_lock);
        if (sessions.contains(ret.sess_id))
        {
            ret.status = SESS_START_ERR_EXISTS;
            if (req.proactive) ProactiveSessResultUpdate(req, false);
            return ret;
        }
    }

    std::unique_ptr<CfmSession> sess;

    switch (req.type)
    {
    case SESS_SLM:
        sess = make_unique<CfmSessionSLM>(
            ret.sess_id, m_timer_wheel, req, lmep_oper);
        break;
    case SESS_DMM:
        sess = make_unique<CfmSessionDMM>(
            ret.sess_id, m_timer_wheel, req, lmep_oper);
        break;
    case SESS_LTM:
        sess = make_unique<CfmSessionLTM>(
            ret.sess_id, m_timer_wheel, req, lmep_oper);
        break;
    case SESS_LBM:
        sess = make_unique<CfmSessionLBM>(
            ret.sess_id, m_timer_wheel, req, lmep_oper);
        break;
    default:
        ret.status = SESS_START_ERR_UNSUPPORTED;
        return ret;
    }

    if (!sess->IsSupported())
    {
        ret.status = SESS_START_ERR_UNSUPPORTED;
        return ret;
    }

    if (is_zero_ether_addr(&sess->config().dst_mac))
        ret.status = SESS_START_ERR_MISSING_MAC;

    if (req.proactive)
    {
        ProactiveSessOperUpdate(*sess, req, lmep_oper);
        ProactiveSessResultUpdate(req, ret.status == SESS_START_OK);
    }
    else
    {
        OdTestInfoUpdate(*sess, req, lmep_oper);
        OdTestResultStatus(req.type, ret, lmep_oper);
    }

    if (ret.status == SESS_START_OK)
    {
        {
            lock_guard _(m_lock);
            sess->IncRefcount(); // Add Session in hash, increment ref count
            sessions.emplace(ret.sess_id, sess.release());
        }
        CFM_LOG(DN_LOG_INFO,
                "INITIATOR CreateSession insert%s %lx",
                req.proactive.has_value() ? " proactive" : "",
                ret.sess_id);
    }

    return ret;
}

struct OdTestInfoCommonFields
{
    field_id source_md_name;
    field_id source_ma_name;
    field_id source_mep_id;
    field_id source_interface;
    field_id pcp;
    field_id start_time;
    field_id end_time;
    field_id source_mac_address;
    field_id target_mac_address;
    field_id target_type;
    field_id target_mep_id;
    field_id timeout;
};

static inline OdTestInfoCommonFields od_test_infofields(SessionOpcode op)
{
    switch (op)
    {
    case SessionOpcode::SESS_DMM:
        return {
            .source_md_name = OD_FIELD_(DMM(Testinfo), source_md_name),
            .source_ma_name = OD_FIELD_(DMM(Testinfo), source_ma_name),
            .source_mep_id = OD_FIELD_(DMM(Testinfo), source_mep_id),
            .source_interface = OD_FIELD_(DMM(Testinfo), source_interface),
            .pcp = OD_FIELD_(DMM(Testinfo), pcp),
            .start_time = OD_FIELD_(DMM(Testinfo), start_time),
            .end_time = OD_FIELD_(DMM(Testinfo), end_time),
            .source_mac_address = OD_FIELD_(DMM(Testinfo), source_mac_address),
            .target_mac_address = OD_FIELD_(DMM(Testinfo), target_mac_address),
            .target_type = OD_FIELD_(DMM(Testinfo), target_type),
            .target_mep_id = OD_FIELD_(DMM(Testinfo), target_mep_id),
            .timeout = OD_FIELD_(DMM(Testinfo), timeout),
        };
    case SessionOpcode::SESS_SLM:
        return {
            .source_md_name = OD_FIELD_(SLM(Testinfo), source_md_name),
            .source_ma_name = OD_FIELD_(SLM(Testinfo), source_ma_name),
            .source_mep_id = OD_FIELD_(SLM(Testinfo), source_mep_id),
            .source_interface = OD_FIELD_(SLM(Testinfo), source_interface),
            .pcp = OD_FIELD_(SLM(Testinfo), pcp),
            .start_time = OD_FIELD_(SLM(Testinfo), start_time),
            .end_time = OD_FIELD_(SLM(Testinfo), end_time),
            .source_mac_address = OD_FIELD_(SLM(Testinfo), source_mac_address),
            .target_mac_address = OD_FIELD_(SLM(Testinfo), target_mac_address),
            .target_type = OD_FIELD_(SLM(Testinfo), target_type),
            .target_mep_id = OD_FIELD_(SLM(Testinfo), target_mep_id),
            .timeout = OD_FIELD_(SLM(Testinfo), timeout),
        };
    case SessionOpcode::SESS_LTM:
        return {
            .source_md_name = OD_FIELD_(LTM(Testinfo), source_md_name),
            .source_ma_name = OD_FIELD_(LTM(Testinfo), source_ma_name),
            .source_mep_id = OD_FIELD_(LTM(Testinfo), source_mep_id),
            .source_interface = OD_FIELD_(LTM(Testinfo), source_interface),
            .pcp = OD_FIELD_(LTM(Testinfo), pcp),
            .start_time = OD_FIELD_(LTM(Testinfo), start_time),
            .end_time = OD_FIELD_(LTM(Testinfo), end_time),
            .source_mac_address = OD_FIELD_(LTM(Testinfo), source_mac_address),
            .target_mac_address = OD_FIELD_(LTM(Testinfo), target_mac_address),
            .target_type = OD_FIELD_(LTM(Testinfo), target_type),
            .target_mep_id = OD_FIELD_(LTM(Testinfo), target_mep_id),
            .timeout = OD_FIELD_(LTM(Testinfo), timeout),
        };
    case SessionOpcode::SESS_LBM:
        return {
            .source_md_name = OD_FIELD_(LBM(Testinfo), source_md_name),
            .source_ma_name = OD_FIELD_(LBM(Testinfo), source_ma_name),
            .source_mep_id = OD_FIELD_(LBM(Testinfo), source_mep_id),
            .source_interface = OD_FIELD_(LBM(Testinfo), source_interface),
            .pcp = OD_FIELD_(LBM(Testinfo), pcp),
            .start_time = OD_FIELD_(LBM(Testinfo), start_time),
            .end_time = OD_FIELD_(LBM(Testinfo), end_time),
            .source_mac_address = OD_FIELD_(LBM(Testinfo), source_mac_address),
            .target_mac_address = OD_FIELD_(LBM(Testinfo), target_mac_address),
            .target_type = OD_FIELD_(LBM(Testinfo), target_type),
            .target_mep_id = OD_FIELD_(LBM(Testinfo), target_mep_id),
            .timeout = OD_FIELD_(LBM(Testinfo), timeout),
        };
    default:
        __builtin_unreachable();
    }
}

static inline class_id od_testinfo_class(SessionOpcode op)
{
    switch (op)
    {
    case SessionOpcode::SESS_DMM:
        return OD_CLASS_(DMM(Testinfo));
    case SessionOpcode::SESS_SLM:
        return OD_CLASS_(SLM(Testinfo));
    case SessionOpcode::SESS_LTM:
        return OD_CLASS_(LTM(Testinfo));
    case SessionOpcode::SESS_LBM:
        return OD_CLASS_(LBM(Testinfo));
    default:
        __builtin_unreachable();
    }
}

static inline auto od_target_type(SessionOpcode op, bool is_rmep) noexcept
    -> std::pair<field_id, field_id>
{
    switch (op)
    {
    case SessionOpcode::SESS_DMM:
        return {
            OD_FIELD_(DMM(Testinfo), target_type),
            is_rmep ? OD_FIELD_(DMM(TestinfoTargettypeEnum), mep_id)
                    : OD_FIELD_(DMM(TestinfoTargettypeEnum), mac_address),
        };
    case SessionOpcode::SESS_SLM:
        return {
            OD_FIELD_(SLM(Testinfo), target_type),
            is_rmep ? OD_FIELD_(SLM(TestinfoTargettypeEnum), mep_id)
                    : OD_FIELD_(SLM(TestinfoTargettypeEnum), mac_address),
        };
    case SessionOpcode::SESS_LTM:
        return {
            OD_FIELD_(LTM(Testinfo), target_type),
            is_rmep ? OD_FIELD_(LTM(TestinfoTargettypeEnum), mep_id)
                    : OD_FIELD_(LTM(TestinfoTargettypeEnum), mac_address),
        };
    case SessionOpcode::SESS_LBM:
        return {
            OD_FIELD_(LBM(Testinfo), target_type),
            is_rmep ? OD_FIELD_(LBM(TestinfoTargettypeEnum), mep_id)
                    : OD_FIELD_(LBM(TestinfoTargettypeEnum), mac_address),
        };
    default:
        __builtin_unreachable();
    }
}

void OdTestInfoUpdate(const CfmSession& sess,
                      const SessionStartRequest& req,
                      const LMepOper& lmep) noexcept
{
    const auto mep_id_str = fmt::format("{}", lmep.config.mep_id);
    std::array<const char*, 3> lmep_key = {
        lmep.config.md_id.c_str(),
        lmep.config.ma_id.c_str(),
        mep_id_str.c_str(),
    };

    CFM_LOG(DN_LOG_INFO,
            "Updating %s Testinfo session id %lx - MD %s MA %s LMEP %d",
            sess_get_opcode_str(req.type),
            sess.sess_id,
            lmep.config.md_id.c_str(),
            lmep.config.ma_id.c_str(),
            lmep.config.mep_id);

    auto obj_d = corm_obj_new(
        od_testinfo_class(req.type), lmep_key.data(), lmep_key.size());
    dbclient_del(&obj_d, // clear potential old target.mep_id
                 1,
                 E_DBCLIENT_FLAGS_FREE_NON_SENT_MSGS
                     | E_DBCLIENT_FLAGS_REQUEST_RECURSIVE);

    auto obj = corm_obj_new(
        od_testinfo_class(req.type), lmep_key.data(), lmep_key.size());

    if (not obj)
    {
        CFM_LOG(DN_LOG_ERR,
                "Failed to allocate %s Testinfo corm_obj for session id %lx",
                sess_get_opcode_str(req.type),
                sess.sess_id);

        return;
    }

    const auto field = od_test_infofields(req.type);
    // FIELD__source_md_name = 5, /*string*/
    int rc =
        corm_string_set(obj, field.source_md_name, lmep.config.md_id.c_str());
    // FIELD__source_ma_name = 6, /*string*/
    rc |= corm_string_set(obj, field.source_ma_name, lmep.config.ma_id.c_str());
    // FIELD__source_mep_id
    rc |= corm_u16_set(obj, field.source_mep_id, lmep.config.mep_id);
    // FIELD__source_interface
    rc |= corm_string_set(
        obj, field.source_interface, lmep.config.interface_name.c_str());
    // FIELD__pcp
    rc |= corm_u8_set(obj, field.pcp, req.pcp);
    // FIELD__timeout - NOTE(lbuga) hardcoded in CLI at 2 seconds
    rc |= corm_u16_set(obj, field.timeout, 2);

    const auto is_rmep_target = req.rmep_id.has_value();
    // FIELD__target_mep_id = 2, /*uint16_t*/
    if (is_rmep_target)
        rc |= corm_u16_set(obj, field.target_mep_id, *req.rmep_id);

    // FIELD__target_type = 0, /*enum mac_address, mep_id*/
    auto [tt_fld, tt_val] = od_target_type(req.type, is_rmep_target);
    rc |= corm_enum_set(obj, tt_fld, tt_val);

    const std::time_t tm = time(NULL);
    char tbuf[32];
    // ISO 8601
    strftime(tbuf, sizeof(tbuf), "%F %T %z", std::gmtime(&tm));

    // FIELD__start_time
    rc |= corm_string_set(obj, field.start_time, tbuf);
    // FIELD__end_time
    rc |= corm_string_set(obj, field.end_time, "");

    char mac_str[MAC_ADDRESS_LEN] = {};
    ether_format_addr(
        mac_str,
        sizeof(mac_str),
        reinterpret_cast<const ether_addr*>(lmep.src_mac_address.data()));
    // FIELD__source_mac_address
    rc |= corm_string_set(obj, field.source_mac_address, mac_str);

    ether_format_addr(mac_str, sizeof(mac_str), &sess.config().dst_mac);
    // FIELD__target_mac_address
    rc |= corm_string_set(obj, field.target_mac_address, mac_str);

    bool specific_updated = sess.OperTestInfoInitial(obj);

    if (rc || !specific_updated)
    {
        CFM_LOG(DN_LOG_ERR, "Failed corm_set for session id %lx", sess.sess_id);
        corm_obj_destroy(obj);
    }
    else if (auto sent =
                 dbclient_set(&obj, 1, E_DBCLIENT_FLAGS_FREE_NON_SENT_MSGS);
             1 != sent)
        CFM_LOG(
            DN_LOG_ERR, "Failed dbclient_set for session id %lx", sess.sess_id);
}

static inline class_id od_test_results_class(SessionOpcode op)
{
    switch (op)
    {
    case SessionOpcode::SESS_DMM:
        return OD_CLASS_(DMM(Testresults));
    case SessionOpcode::SESS_SLM:
        return OD_CLASS_(SLM(Testresults));
    case SessionOpcode::SESS_LTM:
        return OD_CLASS_(LTM(Testresults));
    case SessionOpcode::SESS_LBM:
        return OD_CLASS_(LBM(Testresults));
    default:
        __builtin_unreachable();
    }
}

static inline auto validity(SessionOpcode op, bool ok) noexcept
    -> std::pair<field_id, field_id>
{
    switch (op)
    {
    case SessionOpcode::SESS_DMM:
        return {
            OD_FIELD_(DMM(Testresults), measurement_validity),
            ok ? OD_FIELD_(DMM(TestresultsMeasurementvalidityEnum), incomplete)
               : OD_FIELD_(DMM(TestresultsMeasurementvalidityEnum), invalid),
        };
    case SessionOpcode::SESS_SLM:
        return {
            OD_FIELD_(SLM(Testresults), measurement_validity),
            ok ? OD_FIELD_(SLM(TestresultsMeasurementvalidityEnum), incomplete)
               : OD_FIELD_(SLM(TestresultsMeasurementvalidityEnum), invalid),
        };
    case SessionOpcode::SESS_LTM:
        return {
            OD_FIELD_(LTM(Testresults), measurement_validity),
            ok ? OD_FIELD_(LTM(TestresultsMeasurementvalidityEnum), incomplete)
               : OD_FIELD_(LTM(TestresultsMeasurementvalidityEnum), invalid),
        };
    case SessionOpcode::SESS_LBM:
        return {
            OD_FIELD_(LBM(Testresults), measurement_validity),
            ok ? OD_FIELD_(LBM(TestresultsMeasurementvalidityEnum), incomplete)
               : OD_FIELD_(LBM(TestresultsMeasurementvalidityEnum), invalid),
        };
    default:
        __builtin_unreachable();
    }
}

void OdTestResultStatus(SessionOpcode type,
                        const SessionStartResponse& resp,
                        const LMepOper& lmep) noexcept
{
    const auto mep_id_str = fmt::format("{}", lmep.config.mep_id);
    std::array<const char*, 3> lmep_key = {
        lmep.config.md_id.c_str(),
        lmep.config.ma_id.c_str(),
        mep_id_str.c_str(),
    };

    const auto ok = resp.status == SessionStartStatus::SESS_START_OK;
    CFM_LOG(
        DN_LOG_INFO,
        "Updating %s Testresults validity '%s' session id %lx - MD %s MA %s "
        "LMEP %d",
        sess_get_opcode_str(type),
        ok ? "incomplete" : "invalid",
        resp.sess_id,
        lmep.config.md_id.c_str(),
        lmep.config.ma_id.c_str(),
        lmep.config.mep_id);

    auto obj_d = corm_obj_new(
        od_test_results_class(type), lmep_key.data(), lmep_key.size());
    dbclient_del(&obj_d, // delete potential entry from previous run
                 1,
                 E_DBCLIENT_FLAGS_FREE_NON_SENT_MSGS
                     | E_DBCLIENT_FLAGS_REQUEST_RECURSIVE);

    auto obj = corm_obj_new(
        od_test_results_class(type), lmep_key.data(), lmep_key.size());

    if (not obj)
    {
        CFM_LOG(DN_LOG_ERR,
                "Failed to allocate %s Testresults corm_obj for session id %lx",
                sess_get_opcode_str(type),
                resp.sess_id);
        return;
    }

    const auto [v_field, v_value] = validity(type, ok);
    // FIELD__Testresults__measurement_validity
    int rc = corm_enum_set(obj, v_field, v_value);

    if (rc)
    {
        CFM_LOG(DN_LOG_ERR, "Failed corm_set for session id %lx", resp.sess_id);
        corm_obj_destroy(obj);
    }
    else if (auto sent =
                 dbclient_set(&obj, 1, E_DBCLIENT_FLAGS_FREE_NON_SENT_MSGS);
             1 != sent)
        CFM_LOG(
            DN_LOG_ERR, "Failed dbclient_set for session id %lx", resp.sess_id);
}

void ProactiveSessOperUpdate(const CfmSession& sess,
                             const SessionStartRequest& req,
                             const LMepOper& lmep) noexcept
{
    class_id cid = req.type == SessionOpcode::SESS_DMM ? PA_CLASS_(P_DMM())
                                                       : PA_CLASS_(P_SLM());

    const char* key[] = {req.proactive->name.c_str()};
    auto obj = corm_obj_new(cid, key, 1);

    if (not obj)
    {
        CFM_LOG(DN_LOG_ERR,
                "Failed to allocate %s Operitems corm_obj for session %s",
                sess_get_opcode_str(req.type),
                req.proactive->name.c_str());

        return;
    }

    field_id src_intf;
    field_id src_mac;
    field_id count;
    field_id intvl;
    field_id tmout;
    field_id pcp;
    field_id session_id;
    field_id uptime;

    switch (req.type)
    {
    case SessionOpcode::SESS_DMM:
        src_intf = PA_FIELD_(P_DMM(), source_interface);
        src_mac = PA_FIELD_(P_DMM(), source_mac_address);
        count = PA_FIELD_(P_DMM(), count);
        intvl = PA_FIELD_(P_DMM(), interval);
        tmout = PA_FIELD_(P_DMM(), timeout);
        pcp = PA_FIELD_(P_DMM(), pcp);
        session_id = PA_FIELD_(P_DMM(), session_id);
        uptime = PA_FIELD_(P_DMM(), session_uptime);
        break;
    case SessionOpcode::SESS_SLM:
        src_intf = PA_FIELD_(P_SLM(), source_interface);
        src_mac = PA_FIELD_(P_SLM(), source_mac_address);
        count = PA_FIELD_(P_SLM(), count);
        intvl = PA_FIELD_(P_SLM(), interval);
        tmout = PA_FIELD_(P_SLM(), timeout);
        pcp = PA_FIELD_(P_SLM(), pcp);
        session_id = PA_FIELD_(P_SLM(), session_id);
        uptime = PA_FIELD_(P_SLM(), session_uptime);
        break;
    default:
        return;
    }

    int rc = corm_string_set(obj, src_intf, lmep.config.interface_name.c_str());
    rc |= corm_uint32_set(obj, session_id, req.proactive->id);

    char mac_str[MAC_ADDRESS_LEN] = {};
    ether_format_addr(
        mac_str,
        sizeof(mac_str),
        reinterpret_cast<const ether_addr*>(lmep.src_mac_address.data()));
    // FIELD__source_mac_address
    rc |= corm_string_set(obj, src_mac, mac_str);

    // FIELD__count
    rc |= corm_u16_set(obj, count, req.pkt_count);
    // FIELD__interval
    rc |= corm_u16_set(obj, intvl, req.interval_ms / 1000);
    // FIELD__pcp
    rc |= corm_u8_set(obj, pcp, req.pcp);

    // FIELD__timeout
    rc |= corm_u16_set(obj, tmout, 2);
    // FIELD__session_uptime
    rc |= corm_timeticks_set(obj, uptime, req.proactive->created_ts);

    if (rc)
    {
        CFM_LOG(DN_LOG_ERR,
                "Failed to allocate %s Operitems corm_obj for session %s",
                sess_get_opcode_str(req.type),
                req.proactive->name.c_str());
        corm_obj_destroy(obj);
    }
    else if (auto sent =
                 dbclient_set(&obj, 1, E_DBCLIENT_FLAGS_FREE_NON_SENT_MSGS);
             1 != sent)
        CFM_LOG(
            DN_LOG_ERR, "Failed dbclient_set for session id %lx", sess.sess_id);
}

static inline auto pa_validity(SessionOpcode op, bool ok) noexcept
    -> std::pair<field_id, field_id>
{
    switch (op)
    {
    case SessionOpcode::SESS_DMM:
    {
        auto v = ok ? PA_FIELD_(P_DMM(MeasurementresultMeasurementvalidityEnum),
                                incomplete)
                    : PA_FIELD_(P_DMM(MeasurementresultMeasurementvalidityEnum),
                                invalid);
        return {PA_FIELD_(P_DMM(MeasurementresultItem), measurement_validity),
                v};
    }
    case SessionOpcode::SESS_SLM:
    {
        auto v = ok ? PA_FIELD_(P_SLM(MeasurementresultMeasurementvalidityEnum),
                                incomplete)
                    : PA_FIELD_(P_SLM(MeasurementresultMeasurementvalidityEnum),
                                invalid);
        return {PA_FIELD_(P_SLM(MeasurementresultItem), measurement_validity),
                v};
    }
    default:
        return {field_id(0), field_id(0)};
    }
}

void ProactiveSessResultUpdate(const SessionStartRequest& req, bool ok) noexcept
{
    static const auto run_once [[maybe_unused]] = [] {
        static const auto PDMM_RES =
            "/drivenets-top/services/performance-monitoring/cfm-tests"
            "/proactive-monitoring/two-way-delay-measurements/test-session"
            "/oper-items/measurement-result";
        static const auto PSLM_RES =
            "/drivenets-top/services/performance-monitoring/cfm-tests"
            "/proactive-monitoring/two-way-synthetic-loss-measurements"
            "/test-session/oper-items/measurement-result";

        dbclient_set_n_for_nlist(PDMM_RES, 10);
        dbclient_set_n_for_nlist(PSLM_RES, 10);

        return true;
    }();

    field_id index;
    field_id start_time;
    field_id end_time;
    class_id cid;

    switch (req.type)
    {
    case SessionOpcode::SESS_DMM:
        cid = PA_CLASS_(P_DMM(MeasurementresultItem));
        index = PA_FIELD_(P_DMM(MeasurementresultItem), index);
        start_time = PA_FIELD_(P_DMM(MeasurementresultItem), start_time);
        end_time = PA_FIELD_(P_DMM(MeasurementresultItem), end_time);
        break;
    case SessionOpcode::SESS_SLM:
        cid = PA_CLASS_(P_SLM(MeasurementresultItem));
        index = PA_FIELD_(P_SLM(MeasurementresultItem), index);
        start_time = PA_FIELD_(P_SLM(MeasurementresultItem), start_time);
        end_time = PA_FIELD_(P_SLM(MeasurementresultItem), end_time);
        break;
    default:
        return;
    }

    const auto idx = fmt::format("{}", req.proactive->result_index);
    std::array<const char*, 2> key = {
        req.proactive->name.c_str(),
        idx.c_str(),
    };

    auto obj = corm_obj_new(cid, key.data(), key.size());

    if (not obj)
    {
        CFM_LOG(DN_LOG_ERR,
                "Failed to allocate %s MeasurementresultItem corm_obj for "
                "session %s result index %d",
                sess_get_opcode_str(req.type),
                req.proactive->name.c_str(),
                req.proactive->result_index);
        return;
    }
    const std::time_t tm = time(NULL);
    char tbuf[32];
    // ISO 8601
    strftime(tbuf, sizeof(tbuf), "%F %T %z", std::gmtime(&tm));

    int rc = corm_u8_set(obj, index, req.proactive->result_index);
    rc |= corm_string_set(obj, start_time, tbuf);
    rc |= corm_string_set(obj, end_time, "");

    const auto [v_field, v_value] = pa_validity(req.type, ok);
    rc |= corm_enum_set(obj, v_field, v_value);

    if (rc)
    {
        CFM_LOG(DN_LOG_ERR,
                "Failed corm_set for session %s result index %d",
                req.proactive->name.c_str(),
                req.proactive->result_index);
        corm_obj_destroy(obj);
    }
    else if (auto sent =
                 dbclient_set(&obj, 1, E_DBCLIENT_FLAGS_FREE_NON_SENT_MSGS);
             1 != sent)
        CFM_LOG(DN_LOG_ERR,
                "Failed dbclient_set for session %s result index %d",
                req.proactive->name.c_str(),
                req.proactive->result_index);
}

void CfmInitiator::HandleEvents(const cfm_event_t& event)
{
    switch (event.base.event_id)
    {
    case CFM_EVENT_START_SESSION:
    {
        StartSession(event.cfm_start_sess.sess_id);
        break;
    }
    case CFM_EVENT_STOP_SESSION:
    {
        StopSession(event.cfm_stop_sess.sess);
        break;
    }
    default:
    {
        CFM_LOG(DN_LOG_ERR, "Invalid initiator event received!");
        return;
    }
    };
}

int CfmInitiator::SendStartSessionEvent(uint64_t sess_id)
{
    auto& cfm_manager_interconnect =
        *cfm::CfmManager::GetInstance().GetInterconnect();
    struct cfm_event_t* cfm_event =
        (typeof(cfm_event))events_dispatcher_allocate_event(
            cfm_manager_interconnect.cfg_manager_commands_dispatcher);

    if (cfm_event == nullptr)
    {
        CFM_LOG(DN_LOG_ERR,
                "Failed to allocate configuration_manager update event");
        return -1;
    }

    cfm_event->base.event_id = CFM_EVENT_START_SESSION;
    cfm_event->cfm_start_sess.sess_id = sess_id;

    int rc = events_dispatcher_send_event(
        cfm_manager_interconnect.cfg_manager_commands_dispatcher,
        cfm_manager_interconnect.cfg_manager_commands,
        &cfm_event->base);

    CFM_LOG(DN_LOG_DEBUG, "start_sess id %lx", sess_id);

    return rc;
}


int CfmInitiator::SendStopSessionEvent(void* sess)
{
    auto& cfm_manager_interconnect =
        *cfm::CfmManager::GetInstance().GetInterconnect();
    struct cfm_event_t* cfm_event =
        (typeof(cfm_event))events_dispatcher_allocate_event(
            cfm_manager_interconnect.cfg_manager_commands_dispatcher);

    if (cfm_event == nullptr)
    {
        CFM_LOG(DN_LOG_ERR,
                "Failed to allocate configuration_manager update event");
        return -1;
    }

    cfm_event->base.event_id = CFM_EVENT_STOP_SESSION;
    cfm_event->cfm_stop_sess.sess = sess;

    int rc = events_dispatcher_send_event(
        cfm_manager_interconnect.cfg_manager_commands_dispatcher,
        cfm_manager_interconnect.cfg_manager_commands,
        &cfm_event->base);

    return rc;
}

static void ResultsTimerCb(stw_tmr_t*, void*) noexcept;

bool CfmInitiator::StartSession(uint64_t sess_id)
{
    CFM_LOG(DN_LOG_INFO, "INITIATOR StartSession %lx", sess_id);

    std::unique_ptr<CfmSession> sess;
    {
        lock_guard _(m_lock);
        auto found = sessions.find(sess_id);
        if (found == sessions.end())
        {
            CFM_LOG(
                DN_LOG_ERR, "INITIATOR StartSession NOT FOUND %lx", sess_id);

            return false;
        }

        sess.reset(found->second);
    }

    SessionStartStatus ret = sess->Start();
    if (ret != SESS_START_OK)
    {
        CFM_LOG(DN_LOG_ERR, "INITIATOR StartSession failed %lx", sess_id);
        lock_guard _(m_lock);
        sessions.erase(sess_id);

        return false;
    }

    stw_tmr_t* results_tm = new stw_tmr_t; // std::unique_ptr won't work here
    stw_timer_prepare(results_tm);
    int rc = stw_timer_start(
        m_timer_wheel, results_tm, 0, 1000, ResultsTimerCb, sess.get());

    if (RC_STW_OK != rc)
    {
        CFM_LOG(
            DN_LOG_ERR, "INITIATOR StartSession TIMER  failed %lx", sess_id);
        lock_guard _(m_lock);
        sessions.erase(sess_id);
        sess->Stop();
        delete results_tm;

        return false;
    }

    sess.release();

    {
        lock_guard _(m_lock);
        timers.emplace(sess_id, results_tm);
    }

    return true;
}

// Called from cfg_manager thread. Push event to CfmManager
// Remove session from hash to avoid others to reference it
// Keep the session refcount because object will be put in a ring event
SessionStopResponse CfmInitiator::TerminateSession(
    const SessionStopRequest& req)
{
    CfmSession* sess;
    SessionStopResponse ret;
    ret.status = SESS_STOP_ERR;
    ret.sess_id = req.sess_id;

    {
        lock_guard _(m_lock);
        auto found = sessions.find(req.sess_id);
        if (found == sessions.end()) return ret;

        sess = found->second;
        sessions.erase(req.sess_id);
        timers.erase(req.sess_id);
        // Don't decrement refcount of session here, while it is "traveling" in the ring
        // DecRefcount will be called in StopSession, the handler of the ring event.
    }

    SendStopSessionEvent(sess);

    ret.status = SESS_STOP_OK;
    return ret;
}

// Ring event handler. Decrease session refcount, see CfmInitiator::TerminateSession
int CfmInitiator::StopSession(void* sess_p)
{
    CfmSession* sess = static_cast<CfmSession*>(sess_p);
    {
        lock_guard _(m_lock);
        sess->DecRefcount();
    }

    CFM_LOG(DN_LOG_INFO, "Session stop event handle %lx", sess->sess_id);

    return sess->Stop();
}

void CfmInitiator::HandlePacket(wb_pkt* pkt)
{
    auto it_hw = CfmManager::GetInstance().GetOperation().hw_id_to_oam_id.find(
        pkt->cfm_info.lmep_hw_id);
    if (it_hw == CfmManager::GetInstance().GetOperation().hw_id_to_oam_id.end())
    {
        CFM_LOG(DN_LOG_ERR,
                "Could not find hw id 0x%x into hw_id_to_oam_id map",
                pkt->cfm_info.lmep_hw_id);
        return;
    }

    CfmPacket packet(pkt);
    CfmSession* sess;
    uint32_t oam_id = it_hw->second;
    uint64_t sess_id;
    switch (packet.GetOpcode())
    {
    case OAM_OPCODE_SLR:
        sess_id = sess_compute_id(oam_id, SESS_SLM);
        break;
    case OAM_OPCODE_LTR:
        sess_id = sess_compute_id(oam_id, SESS_LTM);
        break;
    case OAM_OPCODE_LBR:
        sess_id = sess_compute_id(oam_id, SESS_LBM);
        break;
    default:
        CFM_LOG(DN_LOG_ERR,
                "INITIATOR Not yet implemented opcode %d",
                packet.GetOpcode());
        return;
    }

    {
        lock_guard _(m_lock);
        auto it_sess = sessions.find(sess_id);
        if (it_sess == sessions.end())
        {
            CFM_LOG(DN_LOG_ERR, "Could not find sess_id 0x%lx", sess_id);
            return;
        }
        sess = it_sess->second;
        sess->IncRefcount();
    }
    sess->HandlePacket(pkt);
    {
        lock_guard _(m_lock);
        sess->DecRefcount();
    }
}

void ResultsTimerCb(stw_tmr_t* results_tm, void* initiator_sess) noexcept
{
    auto& cfm_initiator = CfmManager::GetInstance().GetInitiator();
    CfmSession* sess = static_cast<CfmSession*>(initiator_sess);

    /* One of the following happened:
     * 1. stop_session from RPC called => removed sess from hash, add to ring,
     * handle ring event, decrement ref, call stop_session
     * 2. session has finished => see below handling of SESS_STATE_DONE
     */
    if (sess->state == SESS_STATE_ABORT)
    {
        {
            lock_guard _(cfm_initiator.m_lock);
            if (sess->GetRefcount()) // Still has refs, try again
            {
                CFM_LOG(
                    DN_LOG_INFO, "INITIATOR: REFCOUNT %d", sess->GetRefcount());
                return;
            }
        }
        CFM_LOG(DN_LOG_INFO, "INITIATOR: DELETE sess %lx", sess->sess_id);

        cfm_initiator.pro_sched.DoneSession(sess->sess_id);
        stw_timer_stop(cfm_initiator.m_timer_wheel, results_tm);
        delete results_tm;
        delete sess;
        return;
    }

    sess->PushOperResults();

    /* If sess is done (sent packets, waited for last packets)
     * Also remove from hash so nobody may reference it.
     * The timer will still run, but will not be present in the hash.
     */
    if (sess->state == SESS_STATE_DONE)
    {
        CFM_LOG(DN_LOG_INFO,
                "INITIATOR: STOP sess %lx  state %s",
                sess->sess_id,
                sess_get_state_str(sess->state));

        lock_guard _(cfm_initiator.m_lock);
        cfm_initiator.sessions.erase(sess->sess_id);
        cfm_initiator.timers.erase(sess->sess_id);
        sess->DecRefcount(); // Removed from hash
        sess->Stop();
    }
}

} // namespace cfm
