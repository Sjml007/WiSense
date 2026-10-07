/*
 * WiSense-Twin — Section 14: ESP32 CSI Receiver Firmware (rx_csi_collector)
 *
 * Captures Channel State Information (CSI) from the paired WiSense transmitter (tx_beacon),
 * isolates RF ambient traffic via MAC filtering, and streams compact binary frames over UART.
 *
 * Solves the throughput ceiling and callback blocking identified in Step 1:
 * - 921600 baud serial interface (>50% bandwidth headroom at 100 pps)
 * - Ultra-lean WiFi callback (<2 us) pushing raw records to a FreeRTOS queue
 * - Background UART serialization pinned to Core 1
 * - Compact binary framing (4-byte magic sync word, fixed 28-byte header, CRC-16)
 * - Periodic [RX_STAT] telemetry matching tx_beacon's [TX_STAT] pattern
 */

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <inttypes.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

#include "esp_system.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_mac.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "driver/uart.h"
#include "esp_vfs_dev.h"

#include "csi_extractor.h"

#define TAG "rx_csi"

#ifndef CONFIG_RX_CSI_UART_BAUDRATE
#define CONFIG_RX_CSI_UART_BAUDRATE 921600
#endif

#ifndef CONFIG_RX_CSI_WIFI_CHANNEL
#define CONFIG_RX_CSI_WIFI_CHANNEL 11
#endif

#ifndef CONFIG_RX_CSI_FILTER_MAC
#define CONFIG_RX_CSI_FILTER_MAC "1a:00:00:00:00:00"
#endif

#ifndef CONFIG_RX_CSI_QUEUE_DEPTH
#define CONFIG_RX_CSI_QUEUE_DEPTH 32
#endif

#ifndef CONFIG_RX_CSI_LOG_INTERVAL_SEC
#define CONFIG_RX_CSI_LOG_INTERVAL_SEC 1
#endif

#ifndef CONFIG_RX_CSI_OUTPUT_BINARY
#define CONFIG_RX_CSI_OUTPUT_BINARY 1
#endif

/*
 * Source MAC address filter: matches s_tx_mac from tx_beacon.
 * Initialized to 1a:00:00:00:00:00, updated by parse_mac_filter() if configured.
 */
static uint8_t s_filter_mac[6] = {0x1a, 0x00, 0x00, 0x00, 0x00, 0x00};

/* Broadcast MAC for ESP-NOW peer */
static const uint8_t s_broadcast_mac[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};

/* FreeRTOS queue holding raw CSI records from callback to output task */
static QueueHandle_t s_csi_queue = NULL;

/* Telemetry counters */
static volatile uint32_t s_received_count = 0;
static volatile uint32_t s_queue_overflow_drops = 0;
static volatile uint32_t s_ambient_filtered_count = 0;
static volatile uint32_t s_output_count = 0;
static volatile uint32_t s_rx_seq_counter = 0;

/**
 * @brief Parse MAC filter string from Kconfig into 6-byte array.
 */
static void parse_mac_filter(void)
{
    unsigned int m[6];
    if (sscanf(CONFIG_RX_CSI_FILTER_MAC, "%x:%x:%x:%x:%x:%x",
               &m[0], &m[1], &m[2], &m[3], &m[4], &m[5]) == 6) {
        for (int i = 0; i < 6; i++) {
            s_filter_mac[i] = (uint8_t)m[i];
        }
    } else {
        ESP_LOGW(TAG, "Failed to parse CONFIG_RX_CSI_FILTER_MAC, using fallback 1a:00:00:00:00:00");
    }
}

/**
 * @brief Configure UART0 with ring buffering and custom high baud rate.
 */
static void init_uart(void)
{
    uart_config_t uart_config = {
        .baud_rate  = CONFIG_RX_CSI_UART_BAUDRATE,
        .data_bits  = UART_DATA_8_BITS,
        .parity     = UART_PARITY_DISABLE,
        .stop_bits  = UART_STOP_BITS_1,
        .flow_ctrl  = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };

    ESP_ERROR_CHECK(uart_driver_install(UART_NUM_0, 4096, 4096, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(UART_NUM_0, &uart_config));
    esp_vfs_dev_uart_use_driver(UART_NUM_0);

    ESP_LOGI(TAG, "UART0 driver initialized at %d baud (4KB TX buffer)", CONFIG_RX_CSI_UART_BAUDRATE);
}

/**
 * @brief Initialize Non-Volatile Storage (required for RF/WiFi calibration).
 */
static void init_nvs(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "Erasing NVS flash due to version mismatch or full storage");
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
}

/**
 * @brief Time-critical WiFi CSI Receive Callback.
 *
 * Executed in WiFi task context on Core 0. Must complete in < 2 us to prevent
 * driver buffer overflow.
 *
 * 1. Discards invalid buffers immediately.
 * 2. Compares source MAC against tx_beacon's known MAC (1a:00:00:00:00:00).
 * 3. Extracts beacon metadata (seq_num, sender_ts) from payload offset 15.
 * 4. Copies raw CSI bytes into record structure and pushes to FreeRTOS queue non-blockingly.
 */
static void wifi_csi_rx_cb(void *ctx, wifi_csi_info_t *info)
{
    if (info == NULL || info->buf == NULL || info->len == 0) {
        return;
    }

    /* MAC filtering: immediately discard ambient frames */
    if (memcmp(info->mac, s_filter_mac, 6) != 0) {
        s_ambient_filtered_count++;
        return;
    }

    /* Extract sequence number and timestamp from tx_beacon payload if present */
    uint32_t seq_num = s_rx_seq_counter++;
    uint32_t sender_ts = 0;
    csi_extract_beacon_meta(info->payload, info->payload_len, &seq_num, &sender_ts);

    /* Construct in-memory record */
    csi_record_t record;
    record.header.magic[0] = CSI_FRAME_MAGIC_0;
    record.header.magic[1] = CSI_FRAME_MAGIC_1;
    record.header.magic[2] = CSI_FRAME_MAGIC_2;
    record.header.magic[3] = CSI_FRAME_MAGIC_3;
    record.header.header_len = sizeof(csi_frame_header_t);
    record.header.payload_len = (info->len > CSI_MAX_DATA_LEN) ? CSI_MAX_DATA_LEN : info->len;
    record.header.seq_num = seq_num;
    record.header.rx_timestamp_us = (uint32_t)esp_timer_get_time();
    record.header.sender_timestamp_ms = sender_ts;
    record.header.rssi = info->rx_ctrl.rssi;
    record.header.channel = info->rx_ctrl.channel;
    record.header.noise_floor = info->rx_ctrl.noise_floor;
    record.header.first_word_invalid = info->first_word_invalid ? 1 : 0;
    record.header.sig_mode = (int8_t)info->rx_ctrl.sig_mode;
    record.header.mcs = (int8_t)info->rx_ctrl.mcs;
    record.header.reserved[0] = 0;
    record.header.reserved[1] = 0;

    memcpy(record.csi_data, info->buf, record.header.payload_len);

    /* Non-blocking queue send: if queue is full, record drop and return immediately */
    if (xQueueSend(s_csi_queue, &record, 0) != pdTRUE) {
        s_queue_overflow_drops++;
    } else {
        s_received_count++;
    }
}

/**
 * @brief Initialize WiFi STA, promiscuous mode, and CSI reception.
 */
static void init_wifi_csi(void)
{
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(esp_netif_init());

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));

    /* Configure 40 MHz channel bandwidth for HT40 subcarrier density */
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(ESP_IF_WIFI_STA, WIFI_BW_HT40));
    ESP_ERROR_CHECK(esp_wifi_start());

    /* Disable power saving to ensure continuous radio receiver uptime */
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));

    /* Secondary channel allocation matching transmitter */
    wifi_second_chan_t second_chan = (CONFIG_RX_CSI_WIFI_CHANNEL >= 7)
                                     ? WIFI_SECOND_CHAN_BELOW
                                     : WIFI_SECOND_CHAN_ABOVE;
    ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_RX_CSI_WIFI_CHANNEL, second_chan));

    /* Enable promiscuous mode to observe all incoming 802.11 frames */
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));

    /* Register ESP-NOW broadcast peer matching tx_beacon */
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_set_pmk((const uint8_t *)"pmk1234567890123"));

    esp_now_peer_info_t peer = {
        .channel   = CONFIG_RX_CSI_WIFI_CHANNEL,
        .ifidx     = WIFI_IF_STA,
        .encrypt   = false,
    };
    memcpy(peer.peer_addr, s_broadcast_mac, 6);
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));

    esp_now_rate_config_t rate_config = {
        .phymode = WIFI_PHY_MODE_HT40,
        .rate    = WIFI_PHY_RATE_MCS0_LGI,
        .ersu    = false,
        .dcm     = false,
    };
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(peer.peer_addr, &rate_config));

    /* Configure CSI acquisition parameters */
    wifi_csi_config_t csi_config = {
        .lltf_en           = true,
        .htltf_en          = true,
        .stbc_htltf2_en    = true,
        .ltf_merge_en      = true,
        .channel_filter_en = true,
        .manu_scale        = false,
        .shift             = false,
    };
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_config));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(wifi_csi_rx_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));

    uint8_t sta_mac[6];
    esp_wifi_get_mac(WIFI_IF_STA, sta_mac);
    ESP_LOGI(TAG, "WiFi CSI initialized: channel=%d, STA MAC=" MACSTR ", Filter MAC=" MACSTR,
             CONFIG_RX_CSI_WIFI_CHANNEL, MAC2STR(sta_mac), MAC2STR(s_filter_mac));
}

/**
 * @brief Output Task: pulls CSI records from FreeRTOS queue and transmits
 *        binary frames over UART. Pinned to Core 1.
 */
static void csi_output_task(void *pvParameters)
{
    csi_record_t record;

    while (1) {
        if (xQueueReceive(s_csi_queue, &record, portMAX_DELAY) == pdTRUE) {
#if CONFIG_RX_CSI_OUTPUT_BINARY
            /* Compute CRC-16 over header and raw CSI payload */
            uint16_t crc = csi_compute_crc16(&record.header, record.csi_data, record.header.payload_len);
            csi_frame_trailer_t trailer = {
                .crc16   = crc,
                .trailer = {CSI_FRAME_TRAILER_0, CSI_FRAME_TRAILER_1}
            };

            /* Transmit header, CSI payload, and trailer over UART */
            uart_write_bytes(UART_NUM_0, (const char *)&record.header, sizeof(csi_frame_header_t));
            uart_write_bytes(UART_NUM_0, (const char *)record.csi_data, record.header.payload_len);
            uart_write_bytes(UART_NUM_0, (const char *)&trailer, sizeof(csi_frame_trailer_t));
#endif
            s_output_count++;
        }
    }
}

/**
 * @brief Telemetry Task: periodically outputs [RX_STAT] reception statistics
 *        matching the tx_beacon [TX_STAT] pattern. Pinned to Core 1.
 */
static void rx_stat_task(void *pvParameters)
{
    const TickType_t interval_ticks = pdMS_TO_TICKS(CONFIG_RX_CSI_LOG_INTERVAL_SEC * 1000);
    uint32_t last_count = 0;
    uint32_t last_time_ms = (uint32_t)(esp_timer_get_time() / 1000);

    while (1) {
        vTaskDelay(interval_ticks);

        uint32_t now_ms = (uint32_t)(esp_timer_get_time() / 1000);
        uint32_t current_count = s_received_count;
        uint32_t window_pkts = current_count - last_count;
        uint32_t elapsed_ms = now_ms - last_time_ms;

        float pps = (elapsed_ms > 0) ? ((float)window_pkts * 1000.0f / (float)elapsed_ms) : 0.0f;
        uint32_t free_heap = (uint32_t)esp_get_free_internal_heap_size();

        printf("[RX_STAT] count=%" PRIu32 " | window_pkts=%" PRIu32 " | uptime=%" PRIu32
               " ms | rate=%.1f pps (target=100) | dropped=%" PRIu32
               " | ambient_filtered=%" PRIu32 " | heap=%" PRIu32 "\n",
               current_count, window_pkts, now_ms, pps,
               s_queue_overflow_drops, s_ambient_filtered_count, free_heap);

        last_count = current_count;
        last_time_ms = now_ms;
    }
}

void app_main(void)
{
    ESP_LOGI(TAG, "================ WiSense CSI Receiver Starting ================");
    ESP_LOGI(TAG, "Baud: %d | Channel: %d | Filter MAC: %s | Queue Depth: %d",
             CONFIG_RX_CSI_UART_BAUDRATE, CONFIG_RX_CSI_WIFI_CHANNEL,
             CONFIG_RX_CSI_FILTER_MAC, CONFIG_RX_CSI_QUEUE_DEPTH);

    parse_mac_filter();
    init_uart();
    init_nvs();

    /* Create FreeRTOS queue for CSI records */
    s_csi_queue = xQueueCreate(CONFIG_RX_CSI_QUEUE_DEPTH, sizeof(csi_record_t));
    if (s_csi_queue == NULL) {
        ESP_LOGE(TAG, "Failed to create CSI queue! System halting.");
        return;
    }

    /* Output task pinned to Core 1, priority 10 */
    xTaskCreatePinnedToCore(csi_output_task, "csi_output", 4096, NULL, 10, NULL, 1);

    /* Telemetry task pinned to Core 1, priority 5 */
    xTaskCreatePinnedToCore(rx_stat_task, "rx_stat", 3072, NULL, 5, NULL, 1);

    /* Initialize WiFi and CSI reception on Core 0 */
    init_wifi_csi();

    ESP_LOGI(TAG, "WiSense CSI Receiver initialization complete. Listening for beacons...");
}
