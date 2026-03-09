#pragma once

#include <include/stw_timer.h>
#include <rte_ether.h>

#include <cstring>

#include "cfm_initiator.h"
#include "CfmTypes.hpp"

struct wb_pkt;
struct corm_obj;

namespace cfm {

class LMepOper;

class CfmSession
{
public:
    CfmSession(uint64_t sess_id,
               stw_t* timer_wheel,
               const SessionStartRequest& req,
               const LMepOper& lmep_op);

    virtual ~CfmSession();

    virtual bool IsSupported();
    virtual SessionStartStatus Start() = 0;
    virtual SessionStopStatus Stop() = 0;
    virtual void PushOperResults() = 0;
    virtual void HandlePacket(wb_pkt* pkt);
    virtual bool OperTestInfoInitial(corm_obj*) const = 0;

    // The Refcount functions need to be called under lock
    int IncRefcount();
    int DecRefcount();
    int GetRefcount();

    const uint64_t sess_id;
    volatile SessionState state = SESS_STATE_INITIALIZING;

    struct Config
    {
        Config(const SessionStartRequest& req, const LMepOper&) noexcept;

        uint32_t pkt_count;
        uint32_t pkt_interval_ms;
        uint32_t pkt_size;
        uint32_t pcp;
        uint32_t oam_id;

        uint32_t hw_id;
        uint16_t mep_id;
        uint16_t outer_tag;
        uint16_t outer_tpid;
        uint16_t inner_tag;
        uint16_t inner_tpid;
        MepDirection direction;
        uint8_t level;
        std::optional<Proactive> proactive;
        ether_addr dst_mac = {};
        ether_addr src_mac = {};
    };

    const Config& config() const noexcept { return cfg; }

protected:
    void OperTestresultInvalid(SessionOpcode) noexcept;

    static inline constexpr uint32_t CFM_INITIATOR_WAIT_LAST_PACKET =
        2000; // 2 seconds

    Config cfg;
    stw_t* const m_timer_wheel;
    stw_tmr_t m_timer;
    int refcount = 0; // not atomic, use lock
};

} // namespace cfm

#define ON_DEMAND DrivenetstopServicesPerformancemonitoringCfmtestsOndemandtests
#define PROACTIVE \
    DrivenetstopServicesPerformancemonitoringCfmtestsProactivemonitoring

#define DMM(item) TwowaydelaymeasurementTestresult##item
#define SLM(item) TwowaysyntheticlossmeasurementTestresult##item
#define LTM(item) LinktraceTestresult##item
#define LBM(item) LoopbackTestresult##item

#define _CAT3(a, b, c) __CAT3(a, b, c)
#define __CAT3(a, b, c) a##b##c
#define _CAT4(a, b, c, d) __CAT4(a, b, c, d)
#define __CAT4(a, b, c, d) a##b##c##d

#define OD_CLASS_(type) _CAT3(CLASS_TYPE__, ON_DEMAND, type)
#define OD_FIELD_(type, field) _CAT4(FIELD__, ON_DEMAND, type, __##field)

#define P_DMM(item) TwowaydelaymeasurementsTestsessionOperitems##item
#define P_SLM(item) TwowaysyntheticlossmeasurementsTestsessionOperitems##item

#define PA_CLASS_(type) _CAT3(CLASS_TYPE__, PROACTIVE, type)
#define PA_FIELD_(type, field) _CAT4(FIELD__, PROACTIVE, type, __##field)
