import os
import sys
import getpass
import requests
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
from tqdm import tqdm
import arrow
from assess_objs import CMAssessment, CMQuestion, CMInstructor
from pil_helpers import savePDF, drawFrontPageText, drawTextBasedOnPageList, adjustFontSize


class CMStudent:
    def __init__(self):
        self.session = requests.Session()
        self.cm_course_json = None

    def signIn(self):
        fail_times = 0
        while fail_times < 3:
            tmp = input('CM Email: ')
            if (fail_times == 0) or tmp:
                username = tmp
            password = getpass.getpass('CM Password: ')

            payload = {'user[email]': username, 'user[password]': password}
            cookies = self.session.post('https://app.crowdmark.com/sign-in', data=payload).cookies.get_dict()
            if 'cm_uuid' not in cookies:
                print("✘ Incorrect email or password. Please try again. ✘", file=sys.stderr)
                print("You can press enter to keep the precious CM Email.", file=sys.stderr)
                fail_times += 1
            else:
                print("✔ Email and password have been verified.")
                print()
                return
        print("Login failed. Bye.", file=sys.stderr)
        sys.exit(1)

    def getAllCourses(self):
        payload = {'all': 'true', 'filter[is_student]': 'true'}
        url = 'https://app.crowdmark.com/api/v2/courses'
        r = self.session.get(url, params=payload)
        if r.status_code == 200:
            self.cm_course_json = r.json()
        else:
            print("getAllCourses Failed.", file=sys.stderr)
            sys.exit(1)
        
    def showAllCourses(self):
        print("All the courses records on Crowdmark:")
        for i in range(len(self.cm_course_json['data'])):
            print("[{}] {}".format(i, self.cm_course_json['data'][i]['id']))

    def getCourseNameFromStdin(self):
        d = input("Please enter an index to select a course (type 'q' to quit): ")
        if d == 'q':
            print("Bye.")
            sys.exit()
        idx_selected = int(d)
        return self.cm_course_json['data'][idx_selected]['id']

    def showAllTestsAndAssignments(self, course_name):
        payload = {'filter[course]': course_name}
        url = 'https://app.crowdmark.com/api/v2/student/assignments'
        r = self.session.get(url, params=payload)
        if r.status_code == 200:
            r_dict = r.json()
        else:
            print("showAllTestsAndAssignments Failed.", file=sys.stderr)
            sys.exit(1)

        assessment_id_list = []
        print("All tests & assignments of the course {}:".format(course_name))
        i = 0
        while i < len(r_dict['data']):
            assessment_id_list.append(r_dict['data'][i]['id'])
            print("[{}] {}".format(i, r_dict['data'][i]['relationships']['exam-master']['data']['id']))
            i += 1
        print("[{}] all".format(i))

        return assessment_id_list
    
    def getAssessmentMetadata(self, assessment_id):
        url = 'https://app.crowdmark.com/api/v1/student/results/{}'.format(assessment_id)
        r = self.session.get(url)
        if r.status_code == 200:
            r_dict = r.json()
        else:
            print("getAssessmentMetadata Failed.", file=sys.stderr)
            sys.exit(1)
        
        cma = CMAssessment(assessment_id)
        cma.setAssessmentIdV2(r_dict['included'][0]['id'])

        url = 'https://app.crowdmark.com/api/v2/student/assignments/{}'.format(cma.assessment_id_v2)
        r = self.session.get(url)
        if r.status_code == 200:
            r_dict_v2 = r.json()
        else:
            print("getAssessmentMetadata Failed.", file=sys.stderr)
            sys.exit(1)

        cma.setCourseName(r_dict['included'][1]['attributes']['name'])
        cma.setAssessmentName(r_dict['included'][0]['attributes']['title'])
        if not r_dict['data']['attributes']['total']:
            cma.setScoreAndTotalPoints(0, 0)
        else:
            cma.setScoreAndTotalPoints(
                int(float(r_dict['data']['attributes']['total'])),
                int(float(r_dict['included'][0]['attributes']['total-points']))
            )
        cma.setDate(arrow.get(r_dict['included'][0]['attributes']['marks-sent-at']))
        cmi = CMInstructor(
            r_dict_v2['included'][0]['attributes']['embedded-launch-data']['lis_person_name_full'],
            r_dict_v2['included'][0]['attributes']['embedded-launch-data']['lis_person_contact_email_primary']
        )
        cma.setInstructor(cmi)

        print("Title: {}".format(cma.assessment_name))
        # Add Qs
        q_data = r_dict_v2['data']['relationships']['questions']['data']
        for i in range(len(q_data)):
            assert q_data[i]['type'] == 'assignment-questions'
            Q = CMQuestion(q_data[i]['id'])
            cma.addQ(q_data[i]['id'], Q)
        
        # Add totalPoints and exam page urls
        for i in range(len(r_dict_v2['included'])):
            cm_entry_v2 = r_dict_v2['included'][i]
            cm_type_v2 = cm_entry_v2['type']
            if cm_type_v2 == 'assignment-questions':
                question_id = cm_entry_v2['id']
                cma.id2Q_dict[question_id].setTotalPoints(
                    cm_entry_v2['attributes']['points']
                )
                cma.id2Q_dict[question_id].setSeq(cm_entry_v2['attributes']['sequence'])
            elif cm_type_v2 == 'assignment-pages':
                question_id = str(cm_entry_v2['relationships']['question']['data']['id'])
                page_id = cm_entry_v2['id']
                page_url = cm_entry_v2['attributes']['url']
                cma.id2Q_dict[question_id].addPage(page_id, page_url)

        # Add points and annotations
        for i in range(len(r_dict['included'])):
            cm_entry = r_dict['included'][i]
            cm_type = cm_entry['type']
            if cm_type == 'evaluations':
                question_id = cm_entry['relationships']['exam-question']['data']['id']
                cma.id2Q_dict[question_id].setPoints(
                    cm_entry['attributes']['points']
                )
            elif cm_type == 'annotations':
                pass

        return cma
    
    def downloadAssessment(self, cma, course_dir):
        # PIL image related config
        im_list = []
        font = None
        print("Downloading ... ")

        # Put Qs in order
        num_of_q = len(cma.id2Q_dict)
        question_arr = [None for _ in range(num_of_q)]
        for question_id in cma.id2Q_dict:
            question = cma.id2Q_dict[question_id]
            question_arr[question.seq] = question

        first_page = True
        for question in tqdm(question_arr, unit='questions'):
            page_list = [None for _ in range(question.approximate_num_pages)]
            for pid in question.pid2pageInfo_dict:
                cursor_pos = 0
                r = requests.get(question.pid2pageInfo_dict[pid]['url'])
                if r.status_code != 200:
                    continue

                pil_img = Image.open(BytesIO(r.content))
                if not font:
                    font = adjustFontSize(pil_img)
                if first_page and (question.seq == 0):
                    first_page = False
                    pil_img, cursor_pos = drawFrontPageText(cma, pil_img, font)
                
                pil_img = drawTextBasedOnPageList(cma, pil_img, page_list, 
                    question, cursor_pos, font)

                # Add to page_list first
                idx = question.pid2pageInfo_dict[pid]['seq_approx']
                page_list[idx] = pil_img

            # Add to im_list
            for page in page_list:
                if page is not None:
                    im_list.append(page)
                
        savePDF(cma, im_list, course_dir)
