# Apache Installation Playbook
This is a playbook for a test task. It installs the apache web server on the target system. The name of the target system and the text of the welcome page of the web server are set by default, but can also be set by passing parameters.

## Installation

Clone the repository. Dependencies required for these playbook: Ansible.

## Usage

How to use:

$ cd /etc/ansible  
$ ansible-playbook -i ./acc_test_inv apache_inst_pb.yml  
or  
$ ansible-playbook -i ./acc_test_inv apache_inst_pb.yml --extra-vars "var_text_to_indexhtml=CustomTEXT" "variable_host=CustomClient"

## Contributing

Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.

Please make sure to update the tests as appropriate.

## License

As It Is